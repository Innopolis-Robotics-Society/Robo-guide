"""api_node -- ROS-обвязка над UiServer (Task C, Stage 1).

Держит aiohttp-сервер в отдельном потоке со своим event loop (rclpy крутится
синхронно в основном потоке через `rclpy.spin`, т.е. однопоточный
executor -- все три подписки друг друга не гоняют), мост ROS->web --
``asyncio.run_coroutine_threadsafe`` (design: скопировано с
guide_robot_face/guide_robot_face/face_node.py:75-97). Мост web->ROS --
новый, у face_node прецедента нет (там WS только сервер->клиент): POST-
хендлеры UiServer выполняются на серверном потоке и должны дождаться
ответа rclpy-клиента, чей `rclpy.task.Future` резолвится колбэком на
ГЛАВНОМ потоке -- `_call_ros` ниже прокидывает результат обратно через
`loop.call_soon_threadsafe`, зеркально face_node-мосту.

Пакет **не** второй клиент Nav2 (design "Границы"): единственная
публикация движения-подобного действия -- `/initialpose` (не Twist, не
NavigateToPose), и `/admin_cmd_vel` здесь не упоминается вообще ни в одном
виде -- специально, чтобы grep по пакету (design критерий 14) не находил
ничего лишнего.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import rclpy
from ament_index_python.packages import PackageNotFoundError, get_package_share_directory
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import ClearEntireCostmap
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool, Trigger

from guide_robot_api.command_adapter import CommandAdapter
from guide_robot_api.lib.http_server import ApiServer
from guide_robot_api.lib.operations import ActionRegistry
from guide_robot_api.lib.qos import QOS_MISSION_STATE, QOS_SUPERVISOR_STATE
from guide_robot_api.lib.state_frame import build_frame
from guide_robot_msgs.action import GoToPose, RunTour, Say
from guide_robot_msgs.msg import FaceState, MissionState, SpeakingStatus
from guide_robot_msgs.srv import (
    GetExhibitContent,
    GetExhibitMedia,
    ListLocations,
    ListTours,
    OverrideFace,
    Redirect,
    ResetLocalization,
    SubmitAnswer,
)

_SERVER_START_TIMEOUT_S = 10.0
_SERVER_STOP_TIMEOUT_S = 5.0

# «Сброс локализации»: первая найденная (category=charging) -- откуда робот стартует.
_RESET_LOCATION_IDS = ("parking", "home")

# Путь-заглушка для /media/*, когда guide_robot_semantic_map не установлен
# или не собран (design C7) -- гарантированно не существует, чтобы
# aiohttp.web.static() не подцепил CWD процесса на пустом Path("").
_UNSET_MEDIA_ROOT = Path("/nonexistent/guide_robot_api-media-root-unset")


def _safe_set_result(future: asyncio.Future, value: object) -> None:
    if not future.done():
        future.set_result(value)


def _safe_set_exception(future: asyncio.Future, exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _yaw_from_quaternion(q: object) -> float:
    """Yaw из geometry_msgs/Quaternion (только Z-поворот интересен здесь)."""
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class RobotApiNode(CommandAdapter, Node):
    """Публикует /mission/state+estop+supervisor_state в WS, проксирует команды в ROS."""

    def __init__(self) -> None:
        """Прочитать параметры, поднять UiServer, подписаться, завести клиентов."""
        super().__init__("robot_api")

        try:
            semantic_map_share = Path(get_package_share_directory("guide_robot_semantic_map"))
            default_media_root = str(semantic_map_share / "content" / "media")
        except PackageNotFoundError:
            # Task B может быть не смержена -- не падать, /media/* просто
            # будет 404 (design C7).
            default_media_root = ""

        self.declare_parameter("bind_host", "127.0.0.1")
        self.declare_parameter("http_port", 8091)
        self.declare_parameter("media_root", default_media_root)
        # Общий секрет с guide-launcher: командные роуты принимают только
        # запросы с этим токеном в X-Bridge-Token. Файл лежит в репозитории
        # (примонтирован и на хосте, и в контейнере), вне git.
        self.declare_parameter("bridge_token_file", "")
        self.declare_parameter("service_timeout_s", 5.0)
        self.declare_parameter("mission_state_stale_s", 3.0)
        self.declare_parameter("initialpose_settle_s", 0.5)
        self.declare_parameter("reset_covariance_xyyaw", [0.25, 0.25, 0.06853891945200942])
        self.declare_parameter("tours_language", "ru")
        # Отдельно от tours_language -- у content_server/narration_server
        # тоже свои независимые языковые параметры, не общий (design D2).
        self.declare_parameter("content_language", "ru")
        # Имена сервисов/экшена -- параметры, не хардкод (design C3).
        self.declare_parameter("run_tour_action", "run_tour")
        self.declare_parameter("request_stop_service", "/mission_fsm/request_stop")
        self.declare_parameter("go_home_service", "/mission_fsm/go_home")
        self.declare_parameter("go_to_base_service", "/mission_fsm/go_to_base")
        self.declare_parameter("submit_confirm_service", "/mission_fsm/submit_confirm")
        self.declare_parameter("request_question_service", "/mission_fsm/request_question")
        self.declare_parameter("prompt_listen_service", "/voice_session_manager/prompt_listen")
        self.declare_parameter("list_tours_service", "/location_server/list_tours")
        self.declare_parameter("list_locations_service", "/location_server/list_locations")
        self.declare_parameter(
            "clear_global_costmap_service", "/global_costmap/clear_entirely_global_costmap"
        )
        self.declare_parameter(
            "clear_local_costmap_service", "/local_costmap/clear_entirely_local_costmap"
        )
        self.declare_parameter(
            "get_exhibit_content_service", "/content_server/get_exhibit_content"
        )
        self.declare_parameter("get_exhibit_media_service", "/content_server/get_exhibit_media")

        bind_host = str(self.get_parameter("bind_host").value)
        if bind_host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("Private API must bind to loopback")
        http_port = int(self.get_parameter("http_port").value)
        media_root_str = str(self.get_parameter("media_root").value)
        media_root = Path(media_root_str) if media_root_str else _UNSET_MEDIA_ROOT
        self._service_timeout_s = float(self.get_parameter("service_timeout_s").value)
        self._mission_state_stale_s = float(self.get_parameter("mission_state_stale_s").value)
        self._initialpose_settle_s = float(self.get_parameter("initialpose_settle_s").value)
        self._reset_covariance_xyyaw = [
            float(v) for v in self.get_parameter("reset_covariance_xyyaw").value
        ]
        self._tours_language = str(self.get_parameter("tours_language").value)
        self._content_language = str(self.get_parameter("content_language").value)

        bridge_token = self._read_bridge_token()

        if not media_root.is_dir():
            self.get_logger().warning(
                f"media_root {media_root} не существует -- /media/* будет 404 "
                "до тех пор (Task B не смержена или контент не собран)"
            )

        # -- состояние, обновляемое подписками (главный поток) ---------------
        self._last_mission_msg: MissionState | None = None
        self._mission_received_at_s: float | None = None
        self._estop = False
        self._supervisor_state = ""
        self._seq = 0
        # Манифест слайдов на (exhibit_id, language) -- design D2. Контент
        # неизменен, пока content_server не перезапустят с новыми данными
        # (редкий на рантайме случай для Stage 2) -- без фоновой инвалидации
        # по version, только "кэш есть -- используем, нет -- тянем и кладём".
        self._media_manifest_cache: dict[tuple[str, str], dict[str, Any]] = {}

        # -- сервер в отдельном потоке (копия face_node.py:60-68) -------------
        self._instance_id = str(uuid.uuid4())
        from guide_robot_api.map_adapter import MapAdapter

        self._map = MapAdapter(self)
        self._pose_client = ActionClient(self, GoToPose, "/mission_fsm/go_to_pose")
        self._reset_pose_client = self.create_client(
            ResetLocalization, "/mission_fsm/reset_localization"
        )
        self.declare_parameter(
            "localization_presets", ["parking", "home", "entrance", "robo_guide"]
        )
        self._operations = ActionRegistry()
        self._speech_state = None
        self._face_state = None
        self._last_frame_key = None
        self._requests = queue.SimpleQueue()
        self._request_guard = self.create_guard_condition(self._drain_requests)
        self._server = ApiServer(
            media_root=media_root, bridge_token=bridge_token, dispatch=self.dispatch
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._server_thread = threading.Thread(
            target=self._run_server, args=(bind_host, http_port), daemon=True
        )
        # -- подписки. Три фиксированных системных топика -- не параметры,
        # как и в mission_fsm_node.py (свои клиенты/экшен ниже параметризованы,
        # эти -- те же строки, что и у остальных потребителей по всему репо).
        self.create_subscription(
            MissionState, "/mission/state", self._on_mission_state, QOS_MISSION_STATE
        )
        self._localization_status = {}
        self._localization_received = 0.0
        self.create_subscription(String, "/localization/status", self._on_localization, QOS_MISSION_STATE)
        self.create_subscription(Bool, "/supervisor/estop", self._on_estop, QOS_MISSION_STATE)
        self.create_subscription(
            String, "/supervisor/state", self._on_supervisor_state, QOS_SUPERVISOR_STATE
        )
        # Раз в секунду пересобираем кадр (растёт mission_state_age_s): по молчанию /ws
        # launcher отличает зависший мост от живого.
        self.create_timer(
            1.0, self._heartbeat_frame, clock=Clock(clock_type=ClockType.STEADY_TIME)
        )

        # -- клиенты -----------------------------------------------------------
        self._run_tour_client = ActionClient(
            self, RunTour, str(self.get_parameter("run_tour_action").value)
        )
        self._request_stop_client = self.create_client(
            Trigger, str(self.get_parameter("request_stop_service").value)
        )
        self._go_home_client = self.create_client(
            Trigger, str(self.get_parameter("go_home_service").value)
        )
        self._go_to_base_client = self.create_client(
            Redirect, str(self.get_parameter("go_to_base_service").value)
        )
        self._submit_confirm_client = self.create_client(
            SetBool, str(self.get_parameter("submit_confirm_service").value)
        )
        self._request_question_client = self.create_client(
            Trigger, str(self.get_parameter("request_question_service").value)
        )
        self._prompt_listen_client = self.create_client(
            Trigger, str(self.get_parameter("prompt_listen_service").value)
        )
        self._list_tours_client = self.create_client(
            ListTours, str(self.get_parameter("list_tours_service").value)
        )
        self._list_locations_client = self.create_client(
            ListLocations, str(self.get_parameter("list_locations_service").value)
        )
        self._clear_global_costmap_client = self.create_client(
            ClearEntireCostmap, str(self.get_parameter("clear_global_costmap_service").value)
        )
        self._clear_local_costmap_client = self.create_client(
            ClearEntireCostmap, str(self.get_parameter("clear_local_costmap_service").value)
        )
        self._get_exhibit_content_client = self.create_client(
            GetExhibitContent, str(self.get_parameter("get_exhibit_content_service").value)
        )
        self._get_exhibit_media_client = self.create_client(
            GetExhibitMedia, str(self.get_parameter("get_exhibit_media_service").value)
        )

        # -- публикатор ----------------------------------------------------------
        self._initialpose_pub = self.create_publisher(
            PoseWithCovarianceStamped, "/initialpose", 10
        )

        self._say_client = ActionClient(self, Say, "/say")
        self._extra_clients = {
            "check_idle": self.create_client(Trigger, "/mission_fsm/check_idle"),
            "pause": self.create_client(Trigger, "/mission_fsm/request_pause"),
            "resume": self.create_client(Trigger, "/mission_fsm/request_resume"),
            "redirect": self.create_client(Redirect, "/mission_fsm/redirect"),
            "answer": self.create_client(SubmitAnswer, "/mission_fsm/submit_answer"),
            "face": self.create_client(OverrideFace, "/face/override"),
        }
        self.create_subscription(FaceState, "/face/state", self._on_face, QOS_MISSION_STATE)
        self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speech, QOS_MISSION_STATE
        )
        self._server_thread.start()
        if not self._loop_ready.wait(timeout=_SERVER_START_TIMEOUT_S):
            raise RuntimeError("API server startup timeout")
        if getattr(self, "_server_error", None):
            raise RuntimeError("API server startup failed") from self._server_error

        self.get_logger().info(f"robot API bridge: http://{bind_host}:{http_port}")

    def _read_bridge_token(self) -> str:
        """Прочитать общий секрет; пустой/отсутствующий файл -- отказ стартовать."""
        token_file = str(self.get_parameter("bridge_token_file").value)
        if not token_file:
            self.get_logger().fatal("bridge_token_file не задан -- командные роуты не защитить")
            raise ValueError("bridge_token_file не задан")
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.get_logger().fatal(f"bridge_token_file {path} не прочитан: {exc}")
            raise ValueError(f"bridge_token_file {path} не прочитан") from exc
        if not token:
            self.get_logger().fatal(f"bridge_token_file {path} пуст")
            raise ValueError(f"bridge_token_file {path} пуст")
        return token

    def _run_server(self, host: str, port: int) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._server.start(host, port))
        except Exception as exc:
            self._server_error = exc
            self._loop_ready.set()
            loop.run_until_complete(self._server.stop())
            loop.close()
            return
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._server.stop())
            loop.close()

    # -- ROS -> web (подписки) ------------------------------------------------

    def _on_mission_state(self, msg: MissionState) -> None:
        if self._mission_received_at_s is None:
            self.get_logger().info("mission_fsm: первое /mission/state получено")
        self._last_mission_msg = msg
        self._mission_received_at_s = self._now_s()
        self._push_frame()

    def _on_estop(self, msg: Bool) -> None:
        self._estop = bool(msg.data)
        self._push_frame()

    def _on_supervisor_state(self, msg: String) -> None:
        self._supervisor_state = msg.data
        self._push_frame()

    def _now_s(self) -> float:
        return time.monotonic()

    def _mission_state_age_s(self) -> float | None:
        if self._mission_received_at_s is None:
            return None
        return max(0.0, self._now_s() - self._mission_received_at_s)

    def _heartbeat_frame(self) -> None:
        try:
            self._push_frame(force=True)
        except Exception as exc:
            self.get_logger().warn(f"heartbeat-кадр не собран: {exc!r}", throttle_duration_sec=5.0)

    def _on_localization(self, msg):
        try:
            self._localization_status = json.loads(msg.data)
            self._localization_received = time.monotonic()
        except ValueError:
            self._localization_status = {}

    def localization_state(self):
        if time.monotonic()-self._localization_received > .5:
            return dict(state="lost", reason="monitor_unavailable", motion_blocked=True,
                        reset_required=True, fresh=False)
        return self._localization_status

    def _push_frame(self, force=False) -> None:
        self._seq += 1
        frame = build_frame(
            seq=self._seq,
            mission_msg=self._last_mission_msg,
            estop=self._estop,
            supervisor_state=self._supervisor_state,
            now_s=self._now_s(),
            mission_received_at_s=self._mission_received_at_s,
        )
        frame.update(
            bridge_instance_id=self._instance_id,
            speech=self._speech_state,
            face=self._face_state,
            readiness=self.readiness(),
            map=self._map.state(),
            localization=self.localization_state(),
        )
        key = repr(
            {k: v for k, v in frame.items() if k not in ("seq", "stamp", "mission_state_age_s")}
        )
        if not force and key == self._last_frame_key:
            return
        self._last_frame_key = key
        assert self._loop is not None
        push_fut = asyncio.run_coroutine_threadsafe(self._publish_snapshot(frame), self._loop)
        # push() бежит fire-and-forget с точки зрения rclpy-потока -- без
        # этого колбэка исключение из неё (напр. ошибка отправки клиенту)
        # осело бы в concurrent.futures.Future, которую никто не читает, и
        # пропало бы молча, оставив UI считать, что кадр ушёл.
        push_fut.add_done_callback(self._log_push_exception)

    def _log_push_exception(self, fut: concurrent.futures.Future[None]) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            self.get_logger().error(f"api_server.push() упал: {exc!r}", throttle_duration_sec=5.0)

    # -- web -> ROS мост -------------------------------------------------------

    async def _call_ros(self, make_future: Any, timeout_s: float, *, name: str = "?") -> Any:
        """Вызвать rclpy `*.call_async()`/`send_goal_async()` и дождаться ответа.

        `make_future` передаётся очередью в ROS executor. Результат переносится
        обратно в asyncio через loop.call_soon_threadsafe. Проверка доступности
        сервиса выполняется адаптером до отправки; таймаут после отправки
        означает неопределённый исход команды (HTTP 504), а не отказ ROS.

        `name` -- только для лога (какой именно ROS-вызов идёт/упал/повис),
        на логику не влияет.
        """
        self.get_logger().debug(f"_call_ros[{name}]: старт, timeout={timeout_s}s")
        dispatched = concurrent.futures.Future()
        self._requests.put((make_future, dispatched))
        self._request_guard.trigger()
        ros_future = await asyncio.wait_for(asyncio.wrap_future(dispatched), timeout_s)
        aio_future = self._loop.create_future()  # type: ignore[union-attr]

        def _on_done(f: object) -> None:
            try:
                result = f.result()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 -- переносим исключение как есть
                self._loop.call_soon_threadsafe(_safe_set_exception, aio_future, exc)  # type: ignore[union-attr]
            else:
                self._loop.call_soon_threadsafe(_safe_set_result, aio_future, result)  # type: ignore[union-attr]

        ros_future.add_done_callback(_on_done)
        try:
            result = await asyncio.wait_for(aio_future, timeout=timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            self.get_logger().warning(
                f"_call_ros[{name}]: таймаут за {timeout_s}s -- сервис/экшен-сервер не отвечает"
                " (не поднят, не активирован, или реально завис)"
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- пробрасываем как есть, только логируем
            self.get_logger().error(f"_call_ros[{name}]: упал -- {exc!r}")
            raise
        else:
            self.get_logger().debug(f"_call_ros[{name}]: ответ получен")
            return result
        finally:
            if not ros_future.done():
                ros_future.cancel()

    async def _call_trigger(self, client: Any, *, name: str) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._call_ros(
                lambda: client.call_async(Trigger.Request()), self._service_timeout_s, name=name
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}
        return (200 if response.success else 409), {"message": response.message}

    # -- команды HTTP (выполняются на серверном потоке/loop) ------------------

    async def _on_api_confirm(self, *, answer: str) -> tuple[int, dict[str, Any]]:
        """Ответ на «Идём дальше?» с экрана: yes / stop / question.

        Только в свежем AWAITING_CONFIRM: очередь submit_confirm в mission_fsm
        состояние не проверяет, и «да», нажатое мимо вопроса, сработало бы
        на следующей остановке. «Стоп» -- то же, что голосовое «стоп»/«нет»
        на этот вопрос: закончить тур (робот едет на базу).
        """
        msg = self._last_mission_msg
        received = self._mission_received_at_s
        fresh = received is not None and self._now_s() - received <= self._mission_state_stale_s
        if msg is None or not fresh or msg.state != MissionState.STATE_AWAITING_CONFIRM:
            return 409, {"message": "not_awaiting_confirm"}
        if answer == "question":
            status, body = await self._call_trigger(
                self._request_question_client, name="request_question"
            )
            if status != 200:
                return status, body
            # Окно без имени -- best effort: без него посетитель скажет «Фирая».
            listen_status, _ = await self._call_trigger(
                self._prompt_listen_client, name="prompt_listen"
            )
            return 200, {"message": "", "listening": listen_status == 200}
        try:
            response = await self._call_ros(
                lambda: self._submit_confirm_client.call_async(
                    SetBool.Request(data=answer == "yes")
                ),
                self._service_timeout_s,
                name="submit_confirm",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}
        return (200 if response.success else 409), {"message": response.message}

    async def _on_api_go_home(self) -> tuple[int, dict[str, Any]]:
        return await self._call_trigger(self._go_home_client, name="go_home")

    async def _on_api_go_to_base(self, *, location_id: str) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._call_ros(
                lambda: self._go_to_base_client.call_async(
                    Redirect.Request(location_id=location_id)
                ),
                self._service_timeout_s,
                name="go_to_base",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}
        return (200 if response.accepted else 409), {"message": response.message}

    async def _on_api_localization_reset(self) -> tuple[int, dict[str, Any]]:
        """Сброс AMCL на стартовую позу робота (design C5). Порядок шагов -- см. task doc.

        Стартовая поза -- локация `parking` (стоянка, Innopark), на площадках
        без стоянки -- `home`.

        Гейты 1 и 2 задания (state != IDLE; возраст /mission/state) здесь
        объединены в проверку "нет свежих данных ИЛИ данные говорят про
        активный тур" -- функционально то же самое (без свежих данных
        IDLE всё равно не подтвердить), но с одним чётким сообщением на
        каждый исход вместо пересекающихся.
        """
        age_s = self._mission_state_age_s()
        if self._last_mission_msg is None or age_s is None or age_s > self._mission_state_stale_s:
            return 409, {"message": "mission_state_stale"}
        if self._last_mission_msg.state != MissionState.STATE_IDLE:
            return 409, {"message": "tour_active"}

        try:
            locations_response = await self._call_ros(
                lambda: self._list_locations_client.call_async(
                    ListLocations.Request(category="charging")
                ),
                self._service_timeout_s,
                name="list_locations",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}
        by_id = {loc.id: loc for loc in locations_response.locations}
        home = next((by_id[i] for i in _RESET_LOCATION_IDS if i in by_id), None)
        if home is None:
            # Не подставляем (0,0,0) молча (design C5.3) -- нет записи, нет сброса.
            return 409, {"message": "home_location_missing"}

        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = "map"
        msg.header.stamp = self.get_clock().now().to_msg()
        # location_server уже отдаёт готовый кватернион (yaw посчитан на
        # его стороне, lib/geometry.yaw_to_quaternion_xyzw) -- копируем
        # как есть, без своей тригонометрии (design C5, discrepancy #7).
        msg.pose.pose.position = home.pose.pose.position
        msg.pose.pose.orientation = home.pose.pose.orientation
        cov = [0.0] * 36
        cov[0] = self._reset_covariance_xyyaw[0]
        cov[7] = self._reset_covariance_xyyaw[1]
        cov[35] = self._reset_covariance_xyyaw[2]
        msg.pose.covariance = cov
        self._initialpose_pub.publish(msg)

        await asyncio.sleep(self._initialpose_settle_s)

        if not await self._clear_costmaps():
            return 504, {"message": "localization_reset_costmaps_outcome_unknown"}

        return 200, {
            "message": "",
            "pose": {
                "x": home.pose.pose.position.x,
                "y": home.pose.pose.position.y,
                "yaw": _yaw_from_quaternion(home.pose.pose.orientation),
            },
        }

    async def _clear_costmaps(self) -> bool:
        """Очистить global и local costmap; False, если хотя бы один не ответил вовремя."""
        ok = True
        for client in (self._clear_global_costmap_client, self._clear_local_costmap_client):
            try:
                await self._call_ros(
                    lambda c=client: c.call_async(ClearEntireCostmap.Request()),
                    self._service_timeout_s,
                    name=f"clear_costmap[{client.srv_name}]",
                )
            except (TimeoutError, asyncio.TimeoutError):
                ok = False
                self.get_logger().warning(f"clear costmap: {client.srv_name} не ответил вовремя")
        return ok

    async def _on_api_costmaps_clear(self) -> tuple[int, dict[str, Any]]:
        if not await self._clear_costmaps():
            return 504, {"message": "outcome_unknown"}
        return 200, {"message": ""}

    async def _on_api_media(self, *, exhibit_id: str) -> tuple[int, dict[str, Any]]:
        """Манифест слайдов экспоната (design D2): title + порядок chunk_id + медиа.

        chunk_ids идёт из GetExhibitContent(mode="full") -- ОБЯЗАТЕЛЬНО
        "full", не дефолт content_server (":short", content_server.py:180).
        Порядок в этом списке -- позиционное соответствие chunk_index из
        /mission/state (design D1): narration_server сам всегда просит
        content_mode="full" (mission.yaml:41, дефолт параметра тоже "full"),
        поэтому Narrate.Feedback.chunk_index -- это позиция в ТОМ ЖЕ
        списке. Если content_mode когда-нибудь сменят на "short" на
        стороне narration_server -- это соответствие молча сломается,
        починка вне Stage 2 (design D1: известная, документированная
        хрупкость, не решаемая здесь).

        Неизвестный exhibit_id ничем не отличается от "есть, но пустой" --
        content_server не бросает, отдаёт chunks=[]/title="" (design B).
        Оба случая сводятся к титульной карточке на стороне клиента через
        ту же `select_media`, специального кода тут не нужно.
        """
        cache_key = (exhibit_id, self._content_language)
        cached = self._media_manifest_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 30:
            return 200, cached[1]

        try:
            content_response = await self._call_ros(
                lambda: self._get_exhibit_content_client.call_async(
                    GetExhibitContent.Request(
                        exhibit_id=exhibit_id, mode="full", language=self._content_language
                    )
                ),
                self._service_timeout_s,
                name="get_exhibit_content",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}

        try:
            media_response = await self._call_ros(
                lambda: self._get_exhibit_media_client.call_async(
                    GetExhibitMedia.Request(exhibit_id=exhibit_id, language=self._content_language)
                ),
                self._service_timeout_s,
                name="get_exhibit_media",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 504, {"message": "outcome_unknown"}

        manifest: dict[str, Any] = {
            "title": content_response.title,
            "chunk_ids": [chunk.chunk_id for chunk in content_response.chunks],
            "items": (
                [
                    {
                        "id": item.id,
                        "chunk_id": item.chunk_id,
                        "kind": item.kind,
                        "path": item.path,
                        "duration_s": item.duration_s,
                        "caption": item.caption,
                    }
                    for item in media_response.items
                ]
                if media_response.ok
                else []
            ),
        }
        if media_response.ok and content_response.title and content_response.chunks:
            self._media_manifest_cache[cache_key] = (time.monotonic(), manifest)
        else:
            return 503, {"message": "content_unavailable"}
        return 200, manifest

    # -- завершение -------------------------------------------------------------

    def destroy_node(self) -> None:
        """Остановить UiServer и его поток перед уничтожением ноды."""
        if self._loop is not None and not self._loop.is_closed():
            stop_fut = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
            try:
                stop_fut.result(timeout=_SERVER_STOP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- не мешать остановке ноды
                self.get_logger().warning("ui_server: ошибка при остановке")
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._server_thread.join(timeout=_SERVER_STOP_TIMEOUT_S)
        super().destroy_node()


def main(args: list[str] | None = None) -> None:
    """Точка входа console_script api_node."""
    rclpy.init(args=args)
    node = RobotApiNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
