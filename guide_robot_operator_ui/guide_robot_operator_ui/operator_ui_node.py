"""operator_ui_node -- ROS-обвязка над UiServer (Task C, Stage 1).

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
import math
import threading
import time
from pathlib import Path
from typing import Any

import rclpy
from ament_index_python.packages import PackageNotFoundError, get_package_share_directory
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import ClearEntireCostmap
from rclpy.action import ActionClient
from rclpy.node import Node
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger

from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import MissionState
from guide_robot_msgs.srv import GetExhibitContent, GetExhibitMedia, ListLocations, ListTours
from guide_robot_operator_ui.lib.auth import RfidBackend, make_auth_chain
from guide_robot_operator_ui.lib.command_log import CommandLogSink
from guide_robot_operator_ui.lib.promo_io import load_promo
from guide_robot_operator_ui.lib.qos import QOS_MISSION_STATE
from guide_robot_operator_ui.lib.rfid_link import PySerialPort, RfidLink
from guide_robot_operator_ui.lib.session import SessionManager
from guide_robot_operator_ui.lib.state_frame import build_frame
from guide_robot_operator_ui.lib.ui_server import UiServer

_SERVER_START_TIMEOUT_S = 10.0
_SERVER_STOP_TIMEOUT_S = 5.0

# Путь-заглушка для /media/*, когда guide_robot_semantic_map не установлен
# или не собран (design C7) -- гарантированно не существует, чтобы
# aiohttp.web.static() не подцепил CWD процесса на пустом Path("").
_UNSET_MEDIA_ROOT = Path("/nonexistent/guide_robot_operator_ui-media-root-unset")


def _safe_set_result(future: asyncio.Future, value: object) -> None:
    if not future.done():
        future.set_result(value)


def _safe_set_exception(future: asyncio.Future, exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _yaw_from_quaternion(q: object) -> float:
    """Yaw из geometry_msgs/Quaternion (только Z-поворот интересен здесь)."""
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class OperatorUiNode(Node):
    """Публикует /mission/state+estop+supervisor_state в WS, проксирует команды в ROS."""

    def __init__(self) -> None:
        """Прочитать параметры, поднять UiServer, подписаться, завести клиентов."""
        super().__init__("operator_ui")

        share = Path(get_package_share_directory("guide_robot_operator_ui"))
        try:
            semantic_map_share = Path(get_package_share_directory("guide_robot_semantic_map"))
            default_media_root = str(semantic_map_share / "content" / "media")
        except PackageNotFoundError:
            # Task B может быть не смержена -- не падать, /media/* просто
            # будет 404 (design C7).
            default_media_root = ""

        self.declare_parameter("bind_host", "127.0.0.1")
        self.declare_parameter("http_port", 8091)
        self.declare_parameter("web_root", str(share / "web"))
        self.declare_parameter("media_root", default_media_root)
        # promo/ живёт в этом же пакете (design F1), не semantic_map --
        # дефолт всегда конкретный путь, PackageNotFoundError здесь
        # невозможен в отличие от media_root выше.
        self.declare_parameter("promo_dir", str(share / "promo"))
        self.declare_parameter("promo_interval_s", 10.0)
        self.declare_parameter("service_timeout_s", 5.0)
        self.declare_parameter("mission_state_stale_s", 3.0)
        self.declare_parameter("initialpose_settle_s", 0.5)
        self.declare_parameter("reset_covariance_xyyaw", [0.25, 0.25, 0.06853891945200942])
        self.declare_parameter("tours_language", "ru")
        # Отдельно от tours_language -- у content_server/narration_server
        # тоже свои независимые языковые параметры, не общий (design D2).
        self.declare_parameter("content_language", "ru")
        self.declare_parameter("slide_interval_s", 8.0)
        # -- аутентификация оператора (Task E). Потолок стойкости всей схемы
        # -- PIN, поэтому его дефолт обязан пройти собственную проверку
        # длины ниже: "changeme" -- валидный по длине (>=8) плейсхолдер,
        # чтобы нода поднималась из коробки, а не "0000" (было бы отказом
        # стартовать сразу после проверки, которую этот же коммит вводит).
        self.declare_parameter("operator_pin", "changeme")
        self.declare_parameter("auth_backends", ["rfid", "pin"])
        self.declare_parameter("session_ttl_s", 600.0)
        self.declare_parameter("nonce_ttl_s", 30.0)
        self.declare_parameter("max_failed_attempts", 5)
        self.declare_parameter("lockout_s", 60.0)
        # Дефолт false -- оператор сворачивает панель посмотреть на слайд и
        # не должен логиниться заново (design E2).
        self.declare_parameter("close_session_on_panel_hide", False)
        self.declare_parameter("command_log_dir", "~/.guide_robot/operator_ui")
        # -- RFID (Task E4). Пусто/нет порта -- RFID недоступен, вход по
        # PIN, узел всё равно поднимается (design E5, критерий 15).
        self.declare_parameter("rfid_port", "/dev/rfid0")
        self.declare_parameter("rfid_secret_file", "")
        self.declare_parameter("rfid_timeout_s", 0.3)
        # firmware/rfid_bridge/src/main.cpp:19-22 -- один REQA на challenge
        # не всегда видит неподвижно лежащую карту (наблюдалось 3 подряд
        # no_card перед успехом), опрос в цикле обязателен на этой стороне.
        self.declare_parameter("rfid_retries", 5)
        # Имена сервисов/экшена -- параметры, не хардкод (design C3).
        self.declare_parameter("run_tour_action", "run_tour")
        self.declare_parameter("request_stop_service", "/mission_fsm/request_stop")
        self.declare_parameter("go_home_service", "/mission_fsm/go_home")
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
        http_port = int(self.get_parameter("http_port").value)
        web_root = Path(str(self.get_parameter("web_root").value))
        media_root_str = str(self.get_parameter("media_root").value)
        media_root = Path(media_root_str) if media_root_str else _UNSET_MEDIA_ROOT
        promo_dir = Path(str(self.get_parameter("promo_dir").value))
        promo_root = promo_dir / "media"
        self._promo_interval_s = float(self.get_parameter("promo_interval_s").value)
        self._service_timeout_s = float(self.get_parameter("service_timeout_s").value)
        self._mission_state_stale_s = float(self.get_parameter("mission_state_stale_s").value)
        self._initialpose_settle_s = float(self.get_parameter("initialpose_settle_s").value)
        self._reset_covariance_xyyaw = [
            float(v) for v in self.get_parameter("reset_covariance_xyyaw").value
        ]
        self._tours_language = str(self.get_parameter("tours_language").value)
        self._content_language = str(self.get_parameter("content_language").value)
        self._slide_interval_s = float(self.get_parameter("slide_interval_s").value)

        self._operator_pin = str(self.get_parameter("operator_pin").value)
        # Отказ стартовать, не молчаливая слабая защита (design E3, критерий
        # 9) -- PIN остаётся резервом при отказе RFID-ридера, поэтому его
        # длина -- потолок стойкости ВСЕЙ схемы, не только PIN-пути.
        if len(self._operator_pin) < 8:
            raise ValueError(
                f"operator_pin короче 8 символов ({len(self._operator_pin)}) -- "
                "это потолок стойкости всей схемы аутентификации, см. README.md"
            )
        if self._operator_pin == "changeme":
            self.get_logger().warning(
                'operator_pin -- дефолтный плейсхолдер "changeme", смени перед деплоем'
            )

        auth_backend_names = [str(name) for name in self.get_parameter("auth_backends").value]
        rfid_backend = None
        if "rfid" in auth_backend_names:
            rfid_backend = self._build_rfid_backend()
        self._auth_chain = make_auth_chain(
            auth_backend_names, operator_pin=self._operator_pin, rfid_backend=rfid_backend
        )

        self._session_ttl_s = float(self.get_parameter("session_ttl_s").value)
        self._close_session_on_panel_hide = bool(
            self.get_parameter("close_session_on_panel_hide").value
        )
        self._sessions = SessionManager(
            session_ttl_s=self._session_ttl_s,
            nonce_ttl_s=float(self.get_parameter("nonce_ttl_s").value),
            max_failed_attempts=int(self.get_parameter("max_failed_attempts").value),
            lockout_s=float(self.get_parameter("lockout_s").value),
        )

        command_log_dir = str(self.get_parameter("command_log_dir").value)
        self._command_log = CommandLogSink(command_log_dir)

        # Один раз при старте (design F2/F3) -- тот же принцип, что у
        # _media_manifest_cache ниже: "кэш есть -- используем", без
        # фоновой инвалидации. Никогда не бросает (lib/promo_io.py) --
        # опечатка в promo.yaml не должна валить узел целиком.
        self._promo_items, promo_warnings = load_promo(promo_dir / "promo.yaml", promo_root)
        for warning in promo_warnings:
            self.get_logger().warning(f"promo: {warning}")

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
        self._server = UiServer(
            web_root=web_root,
            media_root=media_root,
            promo_root=promo_root,
            on_tours=self._on_api_tours,
            on_tour_start=self._on_api_tour_start,
            on_tour_stop=self._on_api_tour_stop,
            on_go_home=self._on_api_go_home,
            on_localization_reset=self._on_api_localization_reset,
            on_media=self._on_api_media,
            on_promo=self._on_api_promo,
            on_auth_challenge=self._on_api_auth_challenge,
            on_auth_verify=self._on_api_auth_verify,
            on_auth_logout=self._on_api_auth_logout,
            on_auth_status=self._on_api_auth_status,
            on_auth_check=self._on_api_auth_check,
            on_auth_touch=self._on_api_auth_touch,
            on_command_logged=self._on_api_command_logged,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._server_thread = threading.Thread(
            target=self._run_server, args=(bind_host, http_port), daemon=True
        )
        self._server_thread.start()
        if not self._loop_ready.wait(timeout=_SERVER_START_TIMEOUT_S):
            raise RuntimeError("ui_server не поднялся за отведённое время")

        if self._auth_chain.mock_active:
            # 1 Гц, отдельный от heartbeat /mission/state (design E3,
            # критерий 11): без mission_fsm предупреждение не должно
            # молчать именно тогда, когда оно нужнее всего.
            self.create_timer(1.0, self._warn_mock_auth)

        # -- подписки. Три фиксированных системных топика -- не параметры,
        # как и в mission_fsm_node.py (свои клиенты/экшен ниже параметризованы,
        # эти -- те же строки, что и у остальных потребителей по всему репо).
        self.create_subscription(
            MissionState, "/mission/state", self._on_mission_state, QOS_MISSION_STATE
        )
        self.create_subscription(Bool, "/supervisor/estop", self._on_estop, 10)
        self.create_subscription(String, "/supervisor/state", self._on_supervisor_state, 10)

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

        self.get_logger().info(f"operator_ui: http://{bind_host}:{http_port}, web_root={web_root}")

    def _build_rfid_backend(self) -> RfidBackend:
        """Собрать RfidBackend; отсутствие секрета/порта -- WARN, не отказ узла (design E5).

        Секрет читается из файла (путь -- параметр, файл вне git,
        design E4/E5), не из самого параметра -- он не должен осесть в
        ros2 param dump/логе запуска.
        """
        secret = ""
        secret_file = str(self.get_parameter("rfid_secret_file").value)
        if secret_file:
            secret_path = Path(secret_file).expanduser()
            try:
                secret = secret_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                self.get_logger().warning(
                    f"rfid_secret_file {secret_path} не прочитан ({exc}) -- RFID недоступен"
                )
        else:
            self.get_logger().warning(
                "rfid_secret_file не задан -- RFID недоступен, вход только по PIN"
            )

        link: RfidLink | None = None
        if secret:
            rfid_port = str(self.get_parameter("rfid_port").value)
            rfid_timeout_s = float(self.get_parameter("rfid_timeout_s").value)
            try:
                link = RfidLink(PySerialPort(rfid_port), timeout_s=rfid_timeout_s)
            except OSError as exc:
                self.get_logger().warning(
                    f"rfid_port {rfid_port} не открылся ({exc}) -- RFID недоступен, "
                    "вход по PIN, узел поднимается (design E5)"
                )
            else:
                self.get_logger().info(
                    f"rfid: порт {rfid_port} открыт, timeout={rfid_timeout_s}s"
                )

        rfid_retries = int(self.get_parameter("rfid_retries").value)
        return RfidBackend(link, secret, retries=rfid_retries)

    def _run_server(self, host: str, port: int) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._server.start(host, port))
        self._loop_ready.set()
        loop.run_forever()

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
        return self.get_clock().now().nanoseconds / 1e9

    def _mission_state_age_s(self) -> float | None:
        if self._mission_received_at_s is None:
            return None
        return max(0.0, self._now_s() - self._mission_received_at_s)

    def _push_frame(self) -> None:
        self._seq += 1
        frame = build_frame(
            seq=self._seq,
            mission_msg=self._last_mission_msg,
            estop=self._estop,
            supervisor_state=self._supervisor_state,
            now_s=self._now_s(),
            mission_received_at_s=self._mission_received_at_s,
        )
        assert self._loop is not None
        push_fut = asyncio.run_coroutine_threadsafe(self._server.push(frame), self._loop)
        # push() бежит fire-and-forget с точки зрения rclpy-потока -- без
        # этого колбэка исключение из неё (напр. ошибка отправки клиенту)
        # осело бы в concurrent.futures.Future, которую никто не читает, и
        # пропало бы молча, оставив UI считать, что кадр ушёл.
        push_fut.add_done_callback(self._log_push_exception)

    def _log_push_exception(self, fut: "concurrent.futures.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            self.get_logger().error(f"ui_server.push() упал: {exc!r}", throttle_duration_sec=5.0)

    # -- web -> ROS мост -------------------------------------------------------

    async def _call_ros(self, make_future: Any, timeout_s: float, *, name: str = "?") -> Any:
        """Вызвать rclpy `*.call_async()`/`send_goal_async()` и дождаться ответа.

        `make_future` зовётся на ЭТОМ (серверном) потоке -- безопасно,
        rclpy-клиенты можно дёргать из любого потока. Но колбэк, которым
        резолвится `rclpy.task.Future`, срабатывает на главном потоке
        (там, где крутится executor) -- сюда, на event loop сервера,
        результат переносится через `loop.call_soon_threadsafe`, зеркально
        `face_node.py`'s `run_coroutine_threadsafe` в обратную сторону.
        Таймаут -- единственный источник 503 (design C2): без сервера на
        другом конце `ros_future` просто никогда не резолвится, и это
        неотличимо здесь от "сервер медленный" -- ни то ни другое не
        считается по отдельности, оба ловятся одним `asyncio.wait_for`.

        `name` -- только для лога (какой именно ROS-вызов идёт/упал/повис),
        на логику не влияет.
        """
        self.get_logger().debug(f"_call_ros[{name}]: старт, timeout={timeout_s}s")
        ros_future = make_future()
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
            return 503, {"message": "service_unavailable"}
        return (200 if response.success else 409), {"message": response.message}

    # -- команды HTTP (выполняются на серверном потоке/loop) ------------------

    async def _on_api_tours(self) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._call_ros(
                lambda: self._list_tours_client.call_async(
                    ListTours.Request(language=self._tours_language)
                ),
                self._service_timeout_s,
                name="list_tours",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 503, {"message": "service_unavailable"}
        tours = [{"id": t.id, "name": t.name} for t in response.tours]
        # slide_interval_s едет здесь же, не отдельным endpoint'ом (design
        # D3): не секрет, а /api/tours и так дёргается клиентом ровно один
        # раз при загрузке страницы -- удобное место для статической
        # конфигурации. operator_pin здесь БОЛЬШЕ НЕ ЕДЕТ (design E2) --
        # раньше это позволяло обойти "замок" одним curl по localhost,
        # проверка PIN теперь только на сервере (/api/auth/verify). "auth"
        # -- то немногое об аутентификации, что UI должен знать статически:
        # список реальных методов входа для экрана входа, флаг mock (для
        # несъёмной плашки, design E3) и параметр сворачивания панели.
        return 200, {
            "tours": tours,
            "slide_interval_s": self._slide_interval_s,
            "auth": {
                "mock": self._auth_chain.mock_active,
                "backends": self._auth_chain.available_names(),
                "session_ttl_s": self._session_ttl_s,
                "close_session_on_panel_hide": self._close_session_on_panel_hide,
            },
        }

    async def _on_api_tour_start(self, *, tour_id: str) -> tuple[int, dict[str, Any]]:
        # RunTour.action's bool-поля не несут wire-дефолтов -- комментарии
        # "дефолт true" в .action ничего не задают на деле, невыставленный
        # bool = False. Ставим все четыре явно, как и все существующие
        # клиенты RunTour (mission_control's cli.py/тесты) -- иначе тур
        # стартует немым, без нарратива и без confirm-вопросов.
        goal = RunTour.Goal(
            tour_id=tour_id,
            greet=True,
            narrate=True,
            confirm_between_stops=True,
            return_home=True,
        )
        try:
            goal_handle = await self._call_ros(
                lambda: self._run_tour_client.send_goal_async(goal),
                self._service_timeout_s,
                name="run_tour.send_goal",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 503, {"message": "service_unavailable"}
        if not goal_handle.accepted:
            return 409, {"message": "rejected"}
        return 200, {"message": ""}

    async def _on_api_tour_stop(self) -> tuple[int, dict[str, Any]]:
        return await self._call_trigger(self._request_stop_client, name="request_stop")

    async def _on_api_go_home(self) -> tuple[int, dict[str, Any]]:
        return await self._call_trigger(self._go_home_client, name="go_home")

    async def _on_api_localization_reset(self) -> tuple[int, dict[str, Any]]:
        """Сброс AMCL на позу `home` (design C5). Порядок шагов -- см. task doc.

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
            return 503, {"message": "service_unavailable"}
        home = next((loc for loc in locations_response.locations if loc.id == "home"), None)
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

        for client in (self._clear_global_costmap_client, self._clear_local_costmap_client):
            try:
                await self._call_ros(
                    lambda c=client: c.call_async(ClearEntireCostmap.Request()),
                    self._service_timeout_s,
                    name=f"clear_costmap[{client.srv_name}]",
                )
            except (TimeoutError, asyncio.TimeoutError):
                self.get_logger().warning(
                    f"localization reset: {client.srv_name} не ответил вовремя"
                )

        return 200, {
            "message": "",
            "pose": {
                "x": home.pose.pose.position.x,
                "y": home.pose.pose.position.y,
                "yaw": _yaw_from_quaternion(home.pose.pose.orientation),
            },
        }

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
        if cached is not None:
            return 200, cached

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
            return 503, {"message": "service_unavailable"}

        try:
            media_response = await self._call_ros(
                lambda: self._get_exhibit_media_client.call_async(
                    GetExhibitMedia.Request(exhibit_id=exhibit_id, language=self._content_language)
                ),
                self._service_timeout_s,
                name="get_exhibit_media",
            )
        except (TimeoutError, asyncio.TimeoutError):
            return 503, {"message": "service_unavailable"}

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
        self._media_manifest_cache[cache_key] = manifest
        return 200, manifest

    async def _on_api_promo(self) -> tuple[int, dict[str, Any]]:
        """Манифест промо-петли (design F2) -- {items, promo_interval_s}, без гейта.

        `path`, не `file` -- тот же ключ, что уже отдаёт `_on_api_media`
        для слайдов тура (промо переиспользует тот же рендерер на
        клиенте, design F2, критерий 11).
        """
        return 200, {
            "items": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "path": item.file,
                    "duration_s": item.duration_s,
                    "caption": item.caption,
                }
                for item in self._promo_items
            ],
            "promo_interval_s": self._promo_interval_s,
        }

    # -- аутентификация HTTP (design E2/E3, выполняются на серверном потоке) --

    def _log_command_line(self, **fields: Any) -> None:
        self._command_log.write({"ts": time.time(), **fields})

    def _warn_mock_auth(self) -> None:
        self.get_logger().warning(
            'operator_ui: auth_backends содержит "mock" -- аутентификация '
            "отключена, любой вход успешен (только для стенда без железа)"
        )

    async def _on_api_auth_challenge(self) -> tuple[int, dict[str, Any]]:
        nonce = self._sessions.issue_nonce()
        return 200, {"nonce": nonce, "backends": self._auth_chain.available_names()}

    async def _on_api_auth_verify(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        nonce = kwargs.get("nonce")
        backend_name = kwargs.get("backend")
        if not isinstance(nonce, str) or not nonce:
            return 400, {"message": "invalid_body"}
        if not isinstance(backend_name, str) or not backend_name:
            return 400, {"message": "invalid_body"}

        # Погашен НЕЗАВИСИМО от исхода ниже -- design E2, критерий 5/6.
        if not self._sessions.consume_nonce(nonce):
            return 401, {"error": "invalid_nonce"}

        # mock коротит всю цепочку (design E3) -- логируемый механизм входа
        # обязан отражать это, а не то, что запросил клиент.
        used_backend = "mock" if self._auth_chain.mock_active else backend_name

        lockout_remaining_s = self._sessions.lockout_remaining_s()
        if lockout_remaining_s is not None:
            self._log_command_line(
                event="auth_attempt",
                backend=used_backend,
                operator="",
                ok=False,
                reason="locked_out",
            )
            return 429, {"error": "locked_out", "retry_after_s": lockout_remaining_s}

        if used_backend == "rfid":
            self.get_logger().info("rfid: приложите карту -- жду challenge/response")

        # to_thread: RfidBackend.verify() блокирует до rfid_timeout_s на
        # реальном порте (design E4) -- вызов из event loop сервера
        # напрямую застопорил бы вообще все WS/HTTP на время каждой
        # попытки входа. Pin/MockBackend от этого не страдают -- быстрые.
        started_s = time.monotonic()
        result = await asyncio.to_thread(self._auth_chain.verify, nonce, backend_name, kwargs)
        elapsed_s = time.monotonic() - started_s
        if used_backend == "rfid":
            # reason уже несёт код от ESP (rfid_timeout/rfid_bad_response/
            # rfid_bad_signature/rfid_missing_card/rfid_unavailable/...,
            # см. RfidBackend.verify в lib/auth.py) -- здесь только делаем
            # его видимым в живом логе ноды, а не только в jsonl.
            self.get_logger().info(
                f"rfid: {'успех' if result.ok else 'отказ'} за {elapsed_s:.2f}s"
                f"{'' if result.ok else f', причина={result.reason}'}"
            )

        if not result.ok:
            self._sessions.record_failure()
            self._log_command_line(
                event="auth_attempt",
                backend=used_backend,
                operator="",
                ok=False,
                reason=result.reason,
            )
            body: dict[str, Any] = {"error": "invalid_credentials"}
            # rfid_no_card -- единственная причина, которую стоит отличать
            # в UI ("карту не считало", а не "неверный вход"): это
            # состояние ридера, не попытка подбора учётных данных, ничего
            # чувствительного не раскрывает. Остальные reason (wrong_pin,
            # rfid_bad_signature, ...) наружу нарочно не идут.
            if result.reason == "rfid_no_card":
                body["reason"] = "rfid_no_card"
            return 401, body

        info, evicted = self._sessions.create_session(
            operator=result.operator, backend=used_backend
        )
        if evicted is not None:
            self.get_logger().warning(
                f"operator_ui: сессия {evicted.operator}/{evicted.backend} "
                "вытеснена новым успешным входом"
            )
            self._log_command_line(
                event="session_evicted", operator=evicted.operator, backend=evicted.backend
            )
        self._log_command_line(
            event="auth_attempt",
            backend=used_backend,
            operator=result.operator,
            ok=True,
            reason="",
        )
        return 200, {
            "token": info.token,
            "expires_at": info.expires_at_wall,
            "operator": info.operator,
        }

    async def _on_api_auth_logout(self) -> tuple[int, dict[str, Any]]:
        info = self._sessions.logout()
        if info is not None:
            self._log_command_line(event="logout", operator=info.operator, backend=info.backend)
        return 200, {"ok": True}

    async def _on_api_auth_status(self) -> tuple[int, dict[str, Any]]:
        info = self._sessions.status()
        if info is None:
            return 200, {"active": False, "expires_at": None, "operator": None}
        return 200, {"active": True, "expires_at": info.expires_at_wall, "operator": info.operator}

    async def _on_api_auth_check(self, token: str | None) -> tuple[bool, str]:
        """Гейт-коллбэк UiServer (design E2) -- без продления окна."""
        info = self._sessions.validate(token)
        return (False, "") if info is None else (True, info.operator)

    async def _on_api_auth_touch(self, token: str) -> None:
        """Продлить скользящее окно -- зовётся гейтом только на исход < 400."""
        self._sessions.touch(token)

    async def _on_api_command_logged(self, *, path: str, operator: str, status: int) -> None:
        self.get_logger().info(f"команда {path} -- оператор={operator or '?'}, статус={status}")
        self._log_command_line(event="command", path=path, operator=operator, status=status)

    # -- завершение -------------------------------------------------------------

    def destroy_node(self) -> None:
        """Остановить UiServer и его поток, закрыть лог-файл перед уничтожением ноды."""
        if self._loop is not None:
            stop_fut = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
            try:
                stop_fut.result(timeout=_SERVER_STOP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- не мешать остановке ноды
                self.get_logger().warning("ui_server: ошибка при остановке", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._server_thread.join(timeout=_SERVER_STOP_TIMEOUT_S)
        self._command_log.close()
        super().destroy_node()


def main(args: list[str] | None = None) -> None:
    """Точка входа console_script operator_ui_node."""
    rclpy.init(args=args)
    node = OperatorUiNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
