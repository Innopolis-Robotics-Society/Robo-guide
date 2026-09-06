"""tool_broker -- единственная нода guide_robot_llm с клиентами к mission/semantic_map/voice.

llm_plam.md §3: держит клиентов и валидацию/гейт по состоянию, тестируется
на моках без бэкенда ЛЛМ вообще и остаётся рабочим, если dialog_agent
выключен (§3: "он тестируется на моках без бэкенда ЛЛМ вообще"). Гейт по
состоянию -- здесь, не в FSM (§4): при несовпадении состояния вызывающий
получает внятный `ToolResult(ok=False, ...)` с перечислением того, что
сейчас доступно, а не идёт в ROS за заведомым REJECT.

`call_tool()` не блокируется на действиях, которые реально идут долго
(`RunTour`, `Say`, `Narrate`) -- ждёт только принятия goal-а сервером, не
результата: будущий tool-calling цикл (шаг 5) не должен виснуть на
трёхминутном рассказе. Сервисные вызовы (`pause`/`resume`/`confirm`/
`finish_answer`/read-only справочники) синхронные с таймаутом -- они и так
короткие.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future
from std_srvs.srv import SetBool, Trigger

from guide_robot_llm import matching
from guide_robot_llm.lib.qos import QOS_ASR_TRANSCRIPT, QOS_MISSION_PRESENCE, QOS_MISSION_STATE
from guide_robot_llm.tools import schema, validate
from guide_robot_msgs.action import Narrate, RunTour, Say
from guide_robot_msgs.msg import MissionState, Presence, Transcript
from guide_robot_msgs.srv import CallTool, EstimateRoute, ListLocations, ListTours, SubmitAnswer

__all__ = ["ToolBrokerNode", "ToolResult", "main"]


@dataclass
class ToolResult:
    """Итог `call_tool()` -- то, что вызывающий (тест-скрипт, потом dialog_agent) видит."""

    ok: bool
    message: str = ""
    data: dict = field(default_factory=dict)


def _wait_future(future: Future, context: object, timeout_s: float) -> bool:
    """Дождаться future реальными миллисекундами. True -- успел, False -- таймаут/shutdown.

    Копия хелпера из mission_fsm_node.py/narration_server_node.py -- пакет
    умышленно не зависит от guide_robot_mission_control в рантайме.
    """
    deadline = time.monotonic() + timeout_s
    while rclpy.ok(context=context):
        if future.done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.001)
    return False


class ToolBrokerNode(LifecycleNode):
    """Lifecycle-нода: клиенты mission_fsm/location_server/route_planner/voice + кэш состояния."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Ресурсы ROS захватываются в on_configure."""
        super().__init__("tool_broker", **node_kwargs)

        self.declare_parameter("service_call_timeout_s", 2.0)
        self.declare_parameter("mission_fsm_ns", "/mission_fsm")
        self.declare_parameter("location_server_ns", "/location_server")
        self.declare_parameter("route_planner_ns", "/route_planner")

        self._active = False
        self._state_lock = threading.Lock()
        self._last_mission_state: MissionState | None = None
        self._last_presence: Presence | None = None

        self._run_tour_lock = threading.Lock()
        self._run_tour_goal_handle: object | None = None

        self._cb_reentrant = ReentrantCallbackGroup()

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять клиентов/подписки."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._service_call_timeout_s = float(self.get_parameter("service_call_timeout_s").value)
        mission_fsm_ns = str(self.get_parameter("mission_fsm_ns").value)
        location_server_ns = str(self.get_parameter("location_server_ns").value)
        route_planner_ns = str(self.get_parameter("route_planner_ns").value)

        # -- действия: тур, речь, разовый рассказ вне тура --
        self._run_tour_client = ActionClient(
            self, RunTour, "run_tour", callback_group=self._cb_reentrant
        )
        self._say_client = ActionClient(self, Say, "say", callback_group=self._cb_reentrant)
        self._narrate_client = ActionClient(
            self, Narrate, "narrate", callback_group=self._cb_reentrant
        )

        # -- mission_fsm: тестовые хуки, обёрнутые в ROS-сервисы --
        self._pause_client = self.create_client(
            Trigger, f"{mission_fsm_ns}/request_pause", callback_group=self._cb_reentrant
        )
        self._resume_client = self.create_client(
            Trigger, f"{mission_fsm_ns}/request_resume", callback_group=self._cb_reentrant
        )
        self._confirm_client = self.create_client(
            SetBool, f"{mission_fsm_ns}/submit_confirm", callback_group=self._cb_reentrant
        )
        self._answer_client = self.create_client(
            SubmitAnswer, f"{mission_fsm_ns}/submit_answer", callback_group=self._cb_reentrant
        )

        # -- semantic_map: read-only справочники --
        self._list_locations_client = self.create_client(
            ListLocations,
            f"{location_server_ns}/list_locations",
            callback_group=self._cb_reentrant,
        )
        self._list_tours_client = self.create_client(
            ListTours, f"{location_server_ns}/list_tours", callback_group=self._cb_reentrant
        )
        self._estimate_route_client = self.create_client(
            EstimateRoute,
            f"{route_planner_ns}/estimate_route",
            callback_group=self._cb_reentrant,
        )

        # -- кэш состояния для снимка/гейта --
        self._mission_state_sub = self.create_subscription(
            MissionState,
            "/mission/state",
            self._on_mission_state,
            QOS_MISSION_STATE,
            callback_group=self._cb_reentrant,
        )
        self._presence_sub = self.create_subscription(
            Presence,
            "/mission/presence",
            self._on_presence,
            QOS_MISSION_PRESENCE,
            callback_group=self._cb_reentrant,
        )

        # -- голосовой путь мимо ЛЛМ для confirm/стоп-слов (llm_plam.md §3/§9) --
        self._transcript_sub = self.create_subscription(
            Transcript,
            "/asr/transcript",
            self._on_transcript,
            QOS_ASR_TRANSCRIPT,
            callback_group=self._cb_reentrant,
        )

        # -- call_tool() наружу для другого процесса (dialog_agent, llm_plam.md §4) --
        self._call_tool_srv = self.create_service(
            CallTool, "~/call_tool", self._srv_call_tool, callback_group=self._cb_reentrant
        )

        self.get_logger().info("tool_broker сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Разрешить обработку вызовов инструментов."""
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Запретить новые вызовы инструментов."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить кэш состояния между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        with self._state_lock:
            self._last_mission_state = None
            self._last_presence = None
        with self._run_tour_lock:
            self._run_tour_goal_handle = None

    # -- кэш /mission/state, /mission/presence -------------------------

    def _on_mission_state(self, msg: MissionState) -> None:
        with self._state_lock:
            self._last_mission_state = msg

    def _on_presence(self, msg: Presence) -> None:
        with self._state_lock:
            self._last_presence = msg

    def last_mission_state(self) -> MissionState | None:
        """Последнее полученное `/mission/state`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_mission_state

    def last_presence(self) -> Presence | None:
        """Последнее полученное `/mission/presence`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_presence

    # -- голосовой путь мимо ЛЛМ: confirm/стоп-слово прямо из ASR-финала -----

    def _on_transcript(self, msg: Transcript) -> None:
        """llm_plam.md §9: da/net/дальше/хватит закрываются локально, не ждут ЛЛМ.

        Неуверенный случай -- просто лог; передача ЛЛМ (dialog_agent, шаг 5)
        здесь не реализована, это явная граница этого захода.
        """
        if not msg.is_final or not self._active:
            return
        mission = self.last_mission_state()
        if mission is None:
            return

        if mission.state == MissionState.STATE_AWAITING_CONFIRM:
            is_yes = matching.match_confirm(msg.text)
            if is_yes is None:
                self.get_logger().info(f"confirm: неуверенно ({msg.text!r}), жду ЛЛМ")
                return
            self.get_logger().info(f"confirm: локально распознано -- {'да' if is_yes else 'нет'}")
            self._call_sync(self._confirm_client, SetBool.Request(data=is_yes))
        elif mission.state == MissionState.STATE_ANSWERING:
            if not matching.match_stop_phrase(msg.text):
                self.get_logger().info(f"answering: неуверенно ({msg.text!r}), жду ЛЛМ")
                return
            self.get_logger().info("answering: локально распознано стоп-слово -- SKIP_STOP")
            self._call_sync(
                self._answer_client,
                SubmitAnswer.Request(outcome=SubmitAnswer.Request.OUTCOME_SKIP_STOP),
            )

    # -- call_tool: единственная точка входа для скриптов/dialog_agent -----

    def call_tool(self, name: str, args: dict | None = None) -> ToolResult:
        """Провалидировать и выполнить один вызов инструмента (llm_plam.md §4)."""
        args = args or {}
        if not self._active:
            return ToolResult(ok=False, message="tool_broker не активен")

        mission = self.last_mission_state()
        mission_state = mission.state if mission is not None else MissionState.STATE_IDLE
        tools_allowed = schema.allowed_tools(mission_state)

        known_locations = (
            self._known_location_ids() if _needs_location_whitelist(name) else frozenset()
        )
        known_tours = self._known_tour_ids() if name == "start_tour" else frozenset()
        try:
            validate.validate_call(
                name,
                args,
                tools_allowed=tools_allowed,
                known_location_ids=known_locations,
                known_tour_ids=known_tours,
            )
        except validate.ValidationError as error:
            return ToolResult(ok=False, message=str(error))

        handler = self._HANDLERS.get(name)
        if handler is None:
            return ToolResult(ok=False, message=f"инструмент {name!r} не реализован")
        return handler(self, args)

    def _srv_call_tool(
        self, request: CallTool.Request, response: CallTool.Response
    ) -> CallTool.Response:
        """Обёртка `call_tool()` для `~/call_tool` -- dialog_agent живёт в другом процессе.

        Битый `args_json` -- вина вызывающего (сериализация на его стороне),
        не повод падать здесь: `ok=False` с внятным сообщением, как и любая
        другая `ValidationError`.
        """
        try:
            args = json.loads(request.args_json) if request.args_json else {}
        except json.JSONDecodeError as error:
            response.ok = False
            response.message = f"битый args_json: {error}"
            response.data_json = "{}"
            return response

        result = self.call_tool(request.name, args)
        response.ok = result.ok
        response.message = result.message
        response.data_json = json.dumps(result.data)
        return response

    # -- туры: RunTour, не ждём результата -- только принятия goal-а --------

    def _tool_start_tour(self, args: dict) -> ToolResult:
        goal = RunTour.Goal(
            tour_id=str(args["tour_id"]),
            greet=bool(args.get("greet", True)),
            narrate=bool(args.get("narrate", True)),
            confirm_between_stops=bool(args.get("confirm_between_stops", True)),
            return_home=bool(args.get("return_home", True)),
        )
        return self._send_run_tour(goal)

    def _tool_guide_to(self, args: dict) -> ToolResult:
        goal = RunTour.Goal(
            location_ids=[str(args["location_id"])],
            greet=bool(args.get("greet", False)),
            narrate=bool(args.get("narrate", True)),
            confirm_between_stops=bool(args.get("confirm_between_stops", False)),
            return_home=bool(args.get("return_home", False)),
        )
        return self._send_run_tour(goal)

    def _tool_tour_by_points(self, args: dict) -> ToolResult:
        ids = [str(location_id) for location_id in args["location_ids"]]
        route_result = self._call_sync(
            self._estimate_route_client,
            EstimateRoute.Request(ids=ids, optimize=bool(args.get("optimize", True))),
        )
        if route_result is None:
            return ToolResult(ok=False, message="route_planner недоступен")
        if not route_result.feasible:
            return ToolResult(ok=False, message="маршрут по заданным точкам не построить")
        goal = RunTour.Goal(
            location_ids=list(route_result.ordered_ids),
            greet=bool(args.get("greet", True)),
            narrate=bool(args.get("narrate", True)),
            confirm_between_stops=bool(args.get("confirm_between_stops", True)),
            return_home=bool(args.get("return_home", True)),
        )
        result = self._send_run_tour(goal)
        result.data["ordered_ids"] = list(route_result.ordered_ids)
        result.data["distance_m"] = route_result.distance_m
        return result

    def _send_run_tour(self, goal: RunTour.Goal) -> ToolResult:
        send_future = self._run_tour_client.send_goal_async(goal)
        if not _wait_future(send_future, self.context, self._service_call_timeout_s):
            return ToolResult(ok=False, message="run_tour не ответил вовремя")
        goal_handle = send_future.result()
        if not goal_handle.accepted:
            return ToolResult(ok=False, message="run_tour отклонил цель (уже есть активный тур?)")
        with self._run_tour_lock:
            self._run_tour_goal_handle = goal_handle
        goal_handle.get_result_async().add_done_callback(
            lambda _future, handle=goal_handle: self._on_run_tour_done(handle)
        )
        return ToolResult(ok=True, message="тур начат")

    def _on_run_tour_done(self, goal_handle: object) -> None:
        with self._run_tour_lock:
            if self._run_tour_goal_handle is goal_handle:
                self._run_tour_goal_handle = None

    def _tool_stop_tour(self, args: dict) -> ToolResult:
        del args
        with self._run_tour_lock:
            goal_handle = self._run_tour_goal_handle
        if goal_handle is None:
            return ToolResult(ok=False, message="тур сейчас не активен")
        goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
        return ToolResult(ok=True, message="отмена тура запрошена")

    # -- mission_fsm: короткие сервисы ---------------------------------------

    def _tool_pause(self, args: dict) -> ToolResult:
        del args
        response = self._call_sync(self._pause_client, Trigger.Request())
        if response is None:
            return ToolResult(ok=False, message="mission_fsm недоступен")
        return ToolResult(ok=response.success, message=response.message)

    def _tool_resume(self, args: dict) -> ToolResult:
        del args
        response = self._call_sync(self._resume_client, Trigger.Request())
        if response is None:
            return ToolResult(ok=False, message="mission_fsm недоступен")
        return ToolResult(ok=response.success, message=response.message)

    def _tool_confirm(self, args: dict) -> ToolResult:
        response = self._call_sync(self._confirm_client, SetBool.Request(data=bool(args["yes"])))
        if response is None:
            return ToolResult(ok=False, message="mission_fsm недоступен")
        return ToolResult(ok=response.success, message=response.message)

    def _tool_finish_answer(self, args: dict) -> ToolResult:
        response = self._call_sync(
            self._answer_client, SubmitAnswer.Request(outcome=int(args["outcome"]))
        )
        if response is None:
            return ToolResult(ok=False, message="mission_fsm недоступен")
        return ToolResult(
            ok=response.accepted,
            message=response.message,
            data={"resume_token": response.resume_token},
        )

    # -- речь: fire-and-forget, ждём только принятия goal-а ------------------

    def _tool_say(self, args: dict) -> ToolResult:
        goal = Say.Goal(
            text=str(args["text"]),
            scope=Say.Goal.SCOPE_DIALOG,
            priority=Say.Goal.PRIORITY_DIALOG,
            interruptible=bool(args.get("interruptible", True)),
        )
        send_future = self._say_client.send_goal_async(goal)
        if not _wait_future(send_future, self.context, self._service_call_timeout_s):
            return ToolResult(ok=False, message="say не ответил вовремя")
        goal_handle = send_future.result()
        if not goal_handle.accepted:
            return ToolResult(ok=False, message="say отклонён")
        return ToolResult(ok=True, message="реплика поставлена в очередь")

    def _tool_tell_about(self, args: dict) -> ToolResult:
        goal = Narrate.Goal(exhibit_id=str(args["exhibit_id"]))
        send_future = self._narrate_client.send_goal_async(goal)
        if not _wait_future(send_future, self.context, self._service_call_timeout_s):
            return ToolResult(ok=False, message="narrate не ответил вовремя")
        goal_handle = send_future.result()
        if not goal_handle.accepted:
            return ToolResult(ok=False, message="narrate отклонён (занят/экспонат не найден)")
        return ToolResult(ok=True, message="рассказ начат")

    # -- read-only справочники ------------------------------------------------

    def _tool_list_locations(self, args: dict) -> ToolResult:
        response = self._call_sync(
            self._list_locations_client,
            ListLocations.Request(
                zone=str(args.get("zone", "")),
                category=str(args.get("category", "")),
                near_only=bool(args.get("near_only", False)),
            ),
        )
        if response is None:
            return ToolResult(ok=False, message="location_server недоступен")
        locations = [_serialize_location(loc) for loc in response.locations]
        return ToolResult(ok=True, data={"locations": locations})

    def _tool_list_tours(self, args: dict) -> ToolResult:
        response = self._call_sync(
            self._list_tours_client, ListTours.Request(language=str(args.get("language", "")))
        )
        if response is None:
            return ToolResult(ok=False, message="location_server недоступен")
        tours = [
            {
                "id": tour.id,
                "name": tour.name,
                "stops": [stop.location_id for stop in tour.stops],
            }
            for tour in response.tours
        ]
        return ToolResult(ok=True, data={"tours": tours})

    def _tool_estimate_route(self, args: dict) -> ToolResult:
        ids = [str(location_id) for location_id in args["ids"]]
        response = self._call_sync(
            self._estimate_route_client,
            EstimateRoute.Request(ids=ids, optimize=bool(args.get("optimize", True))),
        )
        if response is None:
            return ToolResult(ok=False, message="route_planner недоступен")
        return ToolResult(
            ok=response.feasible,
            message="" if response.feasible else "маршрут не построить",
            data={
                "ordered_ids": list(response.ordered_ids),
                "distance_m": response.distance_m,
                "duration_min": response.duration_min,
            },
        )

    _HANDLERS = {
        "start_tour": _tool_start_tour,
        "guide_to": _tool_guide_to,
        "tour_by_points": _tool_tour_by_points,
        "stop_tour": _tool_stop_tour,
        "pause": _tool_pause,
        "resume": _tool_resume,
        "confirm": _tool_confirm,
        "finish_answer": _tool_finish_answer,
        "say": _tool_say,
        "tell_about": _tool_tell_about,
        "list_locations": _tool_list_locations,
        "list_tours": _tool_list_tours,
        "estimate_route": _tool_estimate_route,
    }

    # -- whitelist для validate.py --------------------------------------------

    def _known_location_ids(self) -> frozenset[str]:
        response = self._call_sync(self._list_locations_client, ListLocations.Request())
        if response is None:
            return frozenset()
        return frozenset(loc.id for loc in response.locations)

    def _known_tour_ids(self) -> frozenset[str]:
        response = self._call_sync(self._list_tours_client, ListTours.Request())
        if response is None:
            return frozenset()
        return frozenset(tour.id for tour in response.tours)

    # -- общий синхронный вызов сервиса с таймаутом ---------------------------

    def _call_sync(self, client: object, request: object) -> object | None:
        if not client.wait_for_service(timeout_sec=self._service_call_timeout_s):  # type: ignore[attr-defined]
            return None
        future = client.call_async(request)  # type: ignore[attr-defined]
        if not _wait_future(future, self.context, self._service_call_timeout_s):
            return None
        return future.result()


def _needs_location_whitelist(name: str) -> bool:
    return name in ("guide_to", "tour_by_points")


def _serialize_location(location: object) -> dict:
    return {
        "id": location.id,  # type: ignore[attr-defined]
        "aliases": list(location.aliases),  # type: ignore[attr-defined]
        "zone": location.zone,  # type: ignore[attr-defined]
        "category": location.category,  # type: ignore[attr-defined]
        "is_public": location.is_public,  # type: ignore[attr-defined]
        "x": location.pose.pose.position.x,  # type: ignore[attr-defined]
        "y": location.pose.pose.position.y,  # type: ignore[attr-defined]
    }


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = ToolBrokerNode()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
