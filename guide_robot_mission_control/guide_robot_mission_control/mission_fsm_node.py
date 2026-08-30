"""mission_fsm -- владелец состояния тура, стека прерываний, `/mission/state` (design §5).

Один `RunTour`-goal = один прогон `RootStateMachine.run_tour()` (design
§2.4: "Один активный RunTour goal. Второй — REJECT"), исполняется внутри
`execute_callback`, а не в персистентном фоновом потоке -- тот же паттерн,
что у `narration_server_node._execute_narrate` для `Narrate`: goal
принят -> идёт работа -> кто угодно другой в это время получает REJECT на
уровне `goal_callback`.

`FsmContext` живёт РОВНО один прогон тура; `safety_hold_event`/
`deactivating_event` -- общие для узла и переживают несколько
последовательных туров за одну активацию lifecycle.
"""

from __future__ import annotations

import threading
import time

import rclpy
from geometry_msgs.msg import PoseStamped
from guide_robot_msgs.action import Narrate, RunTour, Say
from guide_robot_msgs.msg import CancelAll, MissionState, SpeakingStatus
from guide_robot_msgs.srv import (
    GetExhibitContent,
    ListLocations,
    ListTours,
    Redirect,
    SubmitAnswer,
)
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool, Trigger

from guide_robot_mission_control.fsm.blackboard_keys import Blackboard, TourPlan
from guide_robot_mission_control.fsm.context import FsmContext
from guide_robot_mission_control.fsm.root_sm import RootStateMachine
from guide_robot_mission_control.lib.qos import (
    QOS_CANCEL_ALL,
    QOS_MISSION_STATE,
    QOS_VOICE_SPEAKING,
)

__all__ = ["MissionFsmNode", "main"]

_POLL_S = 0.001

_STATE_ENUM = {
    "greeting": MissionState.STATE_GREETING,
    "navigating": MissionState.STATE_NAVIGATING,
    "narrating": MissionState.STATE_NARRATING,
    "answering": MissionState.STATE_ANSWERING,
    "awaiting_confirm": MissionState.STATE_AWAITING_CONFIRM,
    "paused": MissionState.STATE_PAUSED,
    "held": MissionState.STATE_HELD,
    "returning": MissionState.STATE_RETURNING,
}

_FINAL_OUTCOME_TO_RESULT = {
    "succeeded": RunTour.Result.OUTCOME_COMPLETED,
    "aborted": RunTour.Result.OUTCOME_ABORTED,
    "canceled": RunTour.Result.OUTCOME_CANCELED,
    "shutdown": RunTour.Result.OUTCOME_CANCELED,
}

# stage2 B2: состояния, где ~/redirect принимается -- зеркало
# `redirect_eligible` состояний fsm/base.py, но по значению MissionState.state
# (сервис синхронно отвечает accepted/message ДО того, как FSM-поток вообще
# заберёт запрос из очереди -- ему нужна отдельная, не-FSM-объектная копия).
_REDIRECT_ALLOWED_STATES = frozenset(
    {
        MissionState.STATE_GREETING,
        MissionState.STATE_NAVIGATING,
        MissionState.STATE_NARRATING,
        MissionState.STATE_ANSWERING,
        MissionState.STATE_AWAITING_CONFIRM,
    }
)

_RUN_TOUR_OUTCOME_NAMES = {
    RunTour.Result.OUTCOME_COMPLETED: "COMPLETED",
    RunTour.Result.OUTCOME_CANCELED: "CANCELED",
    RunTour.Result.OUTCOME_ABORTED: "ABORTED",
    RunTour.Result.OUTCOME_NO_VISITOR: "NO_VISITOR",
}


def _wait_future(future: Future, context: object, timeout_s: float) -> bool:
    """Дождаться future реальными миллисекундами. True -- успел, False -- таймаут/shutdown."""
    deadline = time.monotonic() + timeout_s
    while rclpy.ok(context=context):
        if future.done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)
    return False


class MissionFsmNode(LifecycleNode):
    """Lifecycle-нода: `RunTour`-сервер, nav/say/Narrate-клиенты, `/mission/state`."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Ресурсы ROS захватываются в on_configure."""
        super().__init__("mission_fsm", **node_kwargs)

        self.declare_parameter("answer_max_s", 45.0)
        self.declare_parameter("confirm_timeout_s", 20.0)
        self.declare_parameter("confirm_repeat_max", 1)
        self.declare_parameter("nav_stop_timeout_s", 180.0)
        self.declare_parameter("pause_timeout_s", 120.0)
        self.declare_parameter("held_max_s", 300.0)
        self.declare_parameter("poll_period_s", 0.02)
        self.declare_parameter("hard_stop_result_timeout_s", 1.0)
        self.declare_parameter("heartbeat_s", 1.0)
        self.declare_parameter("service_call_timeout_s", 2.0)
        self.declare_parameter("language", "ru")
        self.declare_parameter("greeting_text", "Здравствуйте! Я проведу для вас экскурсию.")
        self.declare_parameter("confirm_question_text", "Идём дальше?")
        self.declare_parameter("redirect_done_phrase", "Мы на месте. Чем ещё могу помочь?")
        self.declare_parameter("transit_after_s", 6.0)
        self.declare_parameter("home_frame", "map")
        self.declare_parameter("home_pose", [0.0, 0.0, 0.0])

        self._active = False
        self._params: dict[str, object] = {}

        self._safety_hold_event = threading.Event()
        self._deactivating_event = threading.Event()
        self._exec_lock = threading.Lock()
        self._active_ctx: FsmContext | None = None
        self._active_goal_handle: object | None = None
        self._last_state_msg: MissionState | None = None
        self._state_lock = threading.Lock()

        self._estop = False
        self._supervisor_state = ""
        # stage2 блок E: транзитный нарратив молчит, пока кто-то говорит.
        self._speaking = False

        self._cb_reentrant = ReentrantCallbackGroup()
        self._cb_sub = MutuallyExclusiveCallbackGroup()

    # -- lifecycle --------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять клиентов/подписки/сервер `RunTour`."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._params = {
            "answer_max_s": float(self.get_parameter("answer_max_s").value),
            "confirm_timeout_s": float(self.get_parameter("confirm_timeout_s").value),
            "confirm_repeat_max": int(self.get_parameter("confirm_repeat_max").value),
            "nav_stop_timeout_s": float(self.get_parameter("nav_stop_timeout_s").value),
            "pause_timeout_s": float(self.get_parameter("pause_timeout_s").value),
            "held_max_s": float(self.get_parameter("held_max_s").value),
            "poll_period_s": float(self.get_parameter("poll_period_s").value),
            "hard_stop_result_timeout_s": float(
                self.get_parameter("hard_stop_result_timeout_s").value
            ),
            "heartbeat_s": float(self.get_parameter("heartbeat_s").value),
            "service_call_timeout_s": float(self.get_parameter("service_call_timeout_s").value),
            "language": str(self.get_parameter("language").value),
            "greeting_text": str(self.get_parameter("greeting_text").value),
            "confirm_question_text": str(self.get_parameter("confirm_question_text").value),
            "redirect_done_phrase": str(self.get_parameter("redirect_done_phrase").value),
            "transit_after_s": float(self.get_parameter("transit_after_s").value),
            "home_frame": str(self.get_parameter("home_frame").value),
            "home_pose": [float(v) for v in self.get_parameter("home_pose").value],
        }

        self._narrate_client = ActionClient(
            self, Narrate, "narrate", callback_group=self._cb_reentrant
        )
        self._say_client = ActionClient(self, Say, "say", callback_group=self._cb_reentrant)
        self._nav_client = ActionClient(
            self, NavigateToPose, "navigate_to_pose", callback_group=self._cb_reentrant
        )
        self._list_tours_client = self.create_client(ListTours, "/location_server/list_tours")
        self._list_locations_client = self.create_client(
            ListLocations, "/location_server/list_locations"
        )
        # stage2 блок E: транзитный нарратив -- те же чанки, что и обычный
        # контент, тем же сервисом. Адрес хардкожен, как и у
        # narration_server_node.py's _content_client -- этот пакет нигде
        # не параметризует content_server_ns.
        self._content_client = self.create_client(
            GetExhibitContent, "/content_server/get_exhibit_content"
        )

        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_sub,
        )
        self._speaking_sub = self.create_subscription(
            SpeakingStatus,
            "/voice/speaking",
            self._on_speaking_status,
            QOS_VOICE_SPEAKING,
            callback_group=self._cb_sub,
        )
        self._estop_sub = self.create_subscription(
            Bool, "/supervisor/estop", self._on_estop, 10, callback_group=self._cb_sub
        )
        self._supervisor_state_sub = self.create_subscription(
            String,
            "/supervisor/state",
            self._on_supervisor_state,
            10,
            callback_group=self._cb_sub,
        )

        self._state_pub = self.create_lifecycle_publisher(
            MissionState, "/mission/state", QOS_MISSION_STATE
        )

        self._run_tour_server = ActionServer(
            self,
            RunTour,
            "run_tour",
            execute_callback=self._execute_run_tour,
            goal_callback=self._on_run_tour_goal,
            cancel_callback=lambda handle: CancelResponse.ACCEPT,
            callback_group=self._cb_reentrant,
        )

        # Тонкие обёртки над теми же хуками, что уже дёргают тесты (design
        # §5.4 п.6) -- нужны, чтобы `mission_cli` (шаг 8) мог достучаться до
        # них из ОТДЕЛЬНОГО процесса: голые методы узла для этого не годятся,
        # только реальный ROS-интерфейс.
        self._pause_srv = self.create_service(
            Trigger, "~/request_pause", self._srv_request_pause, callback_group=self._cb_reentrant
        )
        self._resume_srv = self.create_service(
            Trigger,
            "~/request_resume",
            self._srv_request_resume,
            callback_group=self._cb_reentrant,
        )
        self._confirm_srv = self.create_service(
            SetBool,
            "~/submit_confirm",
            self._srv_submit_confirm,
            callback_group=self._cb_reentrant,
        )
        self._answer_srv = self.create_service(
            SubmitAnswer,
            "~/submit_answer",
            self._srv_submit_answer,
            callback_group=self._cb_reentrant,
        )
        self._redirect_srv = self.create_service(
            Redirect,
            "~/redirect",
            self._srv_redirect,
            callback_group=self._cb_reentrant,
        )

        self.get_logger().info("mission_fsm сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Разрешить приём RunTour-goal-ов, опубликовать IDLE, поднять heartbeat-таймер.

        design §7: heartbeat `/mission/state` -- "безусловный", 1 Гц, даже
        если тур ни разу не запускался. Без этого первого IDLE-сообщения
        `_last_state_msg` остаётся `None` до первого `RunTour`, и
        `_publish_heartbeat` молчит -- свежеактивированная нода невидима ни
        для `TopicRateWatchdog`, ни для `mission_cli status` (обнаружено
        живым прогоном стека при разработке шага 8, не только по тестам).

        `super().on_activate(state)` -- ПЕРВЫМ, не последним: это он
        активирует managed-сущности (в т.ч. `create_lifecycle_publisher`
        `_state_pub`) -- `publish()` до этого молча не уходит в DDS
        (LifecyclePublisher проверяет свой internal "activated" флаг).
        Публикация IDLE до этого вызова была тихим no-op -- поздний
        подписчик (TRANSIENT_LOCAL) на "первый IDLE" так и не дожидался,
        видел `None` до первого реального перехода состояния (найдено на
        `dialog_agent`, guide_robot_llm/llm_plam.md §5 -- он, в отличие от
        `tool_broker`, не подставляет IDLE по умолчанию при `None`).
        """
        result = super().on_activate(state)
        if result != TransitionCallbackReturn.SUCCESS:
            return result
        self._active = True
        self._safety_hold_event.clear()
        self._deactivating_event.clear()
        self._publish_idle_state()
        self._heartbeat_timer = self.create_timer(
            self._params["heartbeat_s"], self._publish_heartbeat
        )
        return TransitionCallbackReturn.SUCCESS

    def _idle_state_msg(self, *, detail: str = "") -> MissionState:
        msg = MissionState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.state = MissionState.STATE_IDLE
        msg.detail = detail
        return msg

    def _publish_idle_state(self, *, detail: str = "") -> None:
        """Опубликовать IDLE и запомнить его как последнее состояние (heartbeat берёт отсюда).

        Публикация внутри `_state_lock` -- иначе конкурентный heartbeat
        (свой поток `MultiThreadedExecutor`-а) может между `_last_state_msg =
        ...` и `.publish(...)` успеть прочитать ЕЩЁ старое значение и
        опубликовать его ПОСЛЕ этого IDLE, навсегда перекрыв его у
        подписчика (депеша порядка не гарантируется между независимыми
        `.publish()`-вызовами с разных потоков одного паблишера).

        `detail` (stage2 A6) -- человекочитаемая причина ухода в IDLE после
        конца тура (`_execute_run_tour`'s finally); пустая строка для
        обычных вызовов (`on_activate`/`on_deactivate`).
        """
        with self._state_lock:
            self._last_state_msg = self._idle_state_msg(detail=detail)
            self._state_pub.publish(self._last_state_msg)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Форсировать завершение активного тура, опубликовать IDLE, снять heartbeat."""
        self._active = False
        self._deactivating_event.set()
        with self._exec_lock:
            ctx = self._active_ctx
        if ctx is not None:
            deadline = time.monotonic() + 5.0
            while self._active_ctx is not None and time.monotonic() < deadline:
                time.sleep(_POLL_S)
        self._publish_idle_state()
        self.destroy_timer(self._heartbeat_timer)
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить состояние безопасности между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup -- активный тур уже остановлен в on_deactivate."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        self._last_state_msg = None
        self._estop = False
        self._supervisor_state = ""
        self._speaking = False

    # -- голос (stage2 блок E: транзитный нарратив ждёт тишины) --------------

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        self._speaking = bool(msg.speaking)

    def is_speaking(self) -> bool:
        """Последнее известное `/voice/speaking` -- без отдельной проверки протухания.

        Как и `presence_monitor` (см. его `_speaking`), берём значение как
        есть -- пропущенный heartbeat здесь означает лишь один пропущенный
        транзитный чанк, не более, цена ошибки мала.
        """
        return self._speaking

    # -- безопасность (design §5.7, реконсиляция §0.5) -----------------------

    def _on_estop(self, msg: Bool) -> None:
        self._estop = bool(msg.data)
        self._recompute_safety_hold()

    def _on_supervisor_state(self, msg: String) -> None:
        self._supervisor_state = msg.data
        self._recompute_safety_hold()

    def _recompute_safety_hold(self) -> None:
        held = self._estop or self._supervisor_state in ("FAULT", "SHUTDOWN")
        was_held = self._safety_hold_event.is_set()
        if held:
            self._safety_hold_event.set()
        else:
            self._safety_hold_event.clear()
        if held != was_held:
            self.get_logger().info(
                f"safety_hold {'взведён' if held else 'снят'} "
                f"(estop={self._estop}, supervisor_state={self._supervisor_state!r})"
            )

    def _on_cancel_all(self, msg: CancelAll) -> None:
        if not self._active:
            return
        if msg.reason != CancelAll.REASON_BARGE_IN:
            return
        with self._exec_lock:
            ctx = self._active_ctx
        if ctx is not None:
            self.get_logger().info("barge-in получен (/speech/cancel_all)")
            ctx.barge_in_event.set()

    # -- RunTour ----------------------------------------------------------

    def _on_run_tour_goal(self, goal_request: RunTour.Goal) -> GoalResponse:
        if not self._active:
            self.get_logger().warning("RunTour отклонён: mission_fsm не active")
            return GoalResponse.REJECT
        with self._exec_lock:
            busy = self._active_ctx is not None
        if busy:
            self.get_logger().warning("RunTour отклонён: уже есть активный тур")
            return GoalResponse.REJECT
        self.get_logger().info(
            f"RunTour принят: tour_id={goal_request.tour_id!r} "
            f"location_ids={list(goal_request.location_ids)} "
            f"start_index={goal_request.start_index}"
        )
        return GoalResponse.ACCEPT

    def _execute_run_tour(self, goal_handle: object) -> RunTour.Result:
        goal: RunTour.Goal = goal_handle.request  # type: ignore[attr-defined]

        resolved = self._resolve_tour(goal)
        if resolved is None:
            self.get_logger().error(
                f"тур не разрешён: tour_id={goal.tour_id!r} не найден в ListTours "
                "либо location_ids пуст"
            )
            return self._finish_run_tour(
                goal_handle, RunTour.Result.OUTCOME_ABORTED, 0, 0, "tour_not_found"
            )
        stop_ids, exhibit_ids, transit_content_id = resolved
        locations = self._fetch_locations()
        if locations is None:
            self.get_logger().error("location_server недоступен -- тур не может начаться")
            return self._finish_run_tour(
                goal_handle, RunTour.Result.OUTCOME_ABORTED, 0, 0, "location_service_unavailable"
            )
        # stage2 блок E: чанки транзитного нарратива берём ОДИН раз здесь,
        # не при каждом ходе NAVIGATING -- NavigatingState просто читает
        # готовый список из TourPlan. Недоступный content_server/пустой
        # content_id -- транзит молча не звучит, тур это не блокирует.
        transit_chunks = self._fetch_transit_chunks(transit_content_id)

        tour = TourPlan(
            stop_ids=stop_ids,
            exhibit_ids=exhibit_ids,
            tour_id=str(goal.tour_id),
            index=int(goal.start_index),
            greet=bool(goal.greet),
            narrate=bool(goal.narrate),
            confirm_between_stops=bool(goal.confirm_between_stops),
            return_home=bool(goal.return_home),
            transit_content_id=transit_content_id,
            transit_chunks=transit_chunks,
        )
        blackboard = Blackboard(tour=tour)
        ctx = self._make_context(goal_handle, locations)
        with self._exec_lock:
            self._active_ctx = ctx
            self._active_goal_handle = goal_handle

        self.get_logger().info(
            f"тур запущен: {len(stop_ids)} остановок начиная с индекса {tour.index}, "
            f"greet={tour.greet} narrate={tour.narrate} "
            f"confirm_between_stops={tour.confirm_between_stops} return_home={tour.return_home}"
        )
        outcome: str | None = None
        try:
            outcome = RootStateMachine(ctx).run_tour(blackboard)
        finally:
            with self._exec_lock:
                self._active_ctx = None
                self._active_goal_handle = None
            # Без этого /mission/state зависает на последнем опубликованном
            # состоянии тура (обычно RETURNING) навсегда -- ни heartbeat, ни
            # следующий RunTour это не чинят (heartbeat лишь переповторяет
            # last_state_msg). Любой гейт по состоянию (design: клиенты
            # RunTour, будущий tool_broker guide_robot_llm) видел бы "тур
            # ещё активен" даже после его завершения. on_activate/
            # on_deactivate уже публикуют IDLE тем же путём -- здесь третий
            # случай: конец execute_callback вне зависимости от исхода.
            #
            # detail (stage2 A6/B2): CANCELED теперь терминален В МЕСТЕ (без
            # RETURNING, см. root_sm._UNIVERSAL) -- отличить в /mission/state
            # "отменён, стою" от обычного конца тура, а не оставлять detail
            # пустым как раньше. Редирект (blackboard.redirected) -- ещё один
            # путь, минующий RETURNING, с потерей resume_token исходного
            # тура -- отражаем это явно, а не просто пустой строкой.
            # `outcome is None` -- run_tour() бросил исключение (например,
            # RuntimeError на необработанном исходе состояния) -- пустой
            # detail, тур и так не завершился штатно.
            if self._active:
                if outcome == "canceled":
                    idle_detail = "отменён по просьбе посетителя, стою на месте"
                elif outcome == "succeeded" and blackboard.redirected:
                    idle_detail = "тур прерван редиректом (resume_token утрачен), стою на месте"
                else:
                    idle_detail = ""
                self._publish_idle_state(detail=idle_detail)

        # detail в RunTour.Result (stage2 B2): "redirected", а не "succeeded",
        # когда тур закончился редиректом -- отличимо от обычного конца тура
        # тем же способом, что и снаружи в /mission/state.
        redirected_ok = outcome == "succeeded" and blackboard.redirected
        result_detail = "redirected" if redirected_ok else outcome
        result_outcome = _FINAL_OUTCOME_TO_RESULT[outcome]
        return self._finish_run_tour(
            goal_handle,
            result_outcome,
            blackboard.stops_completed,
            blackboard.stops_skipped,
            result_detail,
        )

    def _make_context(self, goal_handle: object, locations: dict[str, PoseStamped]) -> FsmContext:
        # [x, y, yaw]; yaw пока не переносится в quaternion
        home_pose_param = self._params["home_pose"]

        def _home_pose() -> PoseStamped:
            pose = PoseStamped()
            pose.header.frame_id = str(self._params["home_frame"])
            pose.pose.position.x = float(home_pose_param[0])
            pose.pose.position.y = float(home_pose_param[1])
            return pose

        return FsmContext(
            now_ns=lambda: self.get_clock().now().nanoseconds,
            goal_handle=goal_handle,
            narrate_client=self._narrate_client,
            say_client=self._say_client,
            nav_client=self._nav_client,
            resolve_pose=lambda stop_id: locations[stop_id],
            home_pose=_home_pose,
            safety_hold_event=self._safety_hold_event,
            deactivating_event=self._deactivating_event,
            greeting_text=str(self._params["greeting_text"]),
            confirm_question_text=str(self._params["confirm_question_text"]),
            confirm_timeout_s=float(self._params["confirm_timeout_s"]),
            confirm_repeat_max=int(self._params["confirm_repeat_max"]),
            answer_max_s=float(self._params["answer_max_s"]),
            nav_stop_timeout_s=float(self._params["nav_stop_timeout_s"]),
            pause_timeout_s=float(self._params["pause_timeout_s"]),
            held_max_s=float(self._params["held_max_s"]),
            poll_period_s=float(self._params["poll_period_s"]),
            hard_stop_result_timeout_s=float(self._params["hard_stop_result_timeout_s"]),
            known_location_ids=frozenset(locations),
            redirect_done_phrase=str(self._params["redirect_done_phrase"]),
            transit_after_s=float(self._params["transit_after_s"]),
            is_speaking=self.is_speaking,
            on_state_changed=self._on_fsm_state_changed,
            log=self.get_logger().info,
        )

    def _resolve_tour(self, goal: RunTour.Goal) -> tuple[list[str], list[str], str] | None:
        """Разрешить (stop_ids, exhibit_ids, transit_content_id).

        `transit_content_id` -- пусто для явного `RunTour.Goal.location_ids`
        (guide_to/redirect/tour_by_points): у одностопового/ad-hoc плана
        нет "тура" в смысле tours.yaml, транзитный нарратив (stage2 блок E)
        для него не заявлен в спеке.
        """
        if goal.location_ids:
            ids = list(goal.location_ids)
            return ids, ids, ""
        if not self._list_tours_client.wait_for_service(
            timeout_sec=self._params["service_call_timeout_s"]
        ):
            return None
        future = self._list_tours_client.call_async(
            ListTours.Request(language=str(self._params["language"]))
        )
        if not _wait_future(future, self.context, self._params["service_call_timeout_s"]):
            return None
        tour = next((t for t in future.result().tours if t.id == goal.tour_id), None)
        if tour is None or not tour.stops:
            return None
        return (
            [stop.location_id for stop in tour.stops],
            [stop.exhibit_id for stop in tour.stops],
            str(tour.transit_content_id),
        )

    def _fetch_transit_chunks(self, transit_content_id: str) -> list[str]:
        """Забрать все чанки транзитного контента (stage2 блок E), пусто при любой неудаче.

        `mode="full"` -- транзитные чанки в content/*.yaml помечены
        `level: short`, но нам нужны ВСЕ, не только уровень short
        (`select_chunks("full")` отдаёт все чанки независимо от level).
        Недоступный content_server/отсутствующий exhibit_id -- не повод
        ронять тур, просто не будет транзитного нарратива.
        """
        if not transit_content_id:
            return []
        if not self._content_client.wait_for_service(
            timeout_sec=self._params["service_call_timeout_s"]
        ):
            return []
        future = self._content_client.call_async(
            GetExhibitContent.Request(
                exhibit_id=transit_content_id,
                mode="full",
                language=str(self._params["language"]),
            )
        )
        if not _wait_future(future, self.context, self._params["service_call_timeout_s"]):
            return []
        return [chunk.text for chunk in future.result().chunks]

    def _fetch_locations(self) -> dict[str, PoseStamped] | None:
        if not self._list_locations_client.wait_for_service(
            timeout_sec=self._params["service_call_timeout_s"]
        ):
            return None
        future = self._list_locations_client.call_async(ListLocations.Request())
        if not _wait_future(future, self.context, self._params["service_call_timeout_s"]):
            return None
        return {loc.id: loc.pose for loc in future.result().locations}

    def _finish_run_tour(
        self,
        goal_handle: object,
        outcome: int,
        stops_completed: int,
        stops_skipped: int,
        detail: str,
    ) -> RunTour.Result:
        result = RunTour.Result(
            outcome=outcome,
            stops_completed=stops_completed,
            stops_skipped=stops_skipped,
            detail=detail,
        )
        message = (
            f"тур завершён: outcome={_RUN_TOUR_OUTCOME_NAMES.get(outcome, outcome)} "
            f"completed={stops_completed} skipped={stops_skipped} detail={detail!r}"
        )
        # rclpy запрещает менять уровень одного и того же логгера между
        # вызовами (`ValueError: Logger severity cannot be changed between
        # calls`) -- раньше он менялся через переменную с методом
        # (`log = ...info if ... else ...warning`), что и роняло
        # `_execute_run_tour` без результата на goal (CLAUDE_CODE_TASK.md п.6).
        if outcome == RunTour.Result.OUTCOME_COMPLETED:
            self.get_logger().info(message)
        else:
            self.get_logger().warning(message)
        if outcome == RunTour.Result.OUTCOME_ABORTED:
            goal_handle.abort()  # type: ignore[attr-defined]
        elif goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
            goal_handle.canceled()  # type: ignore[attr-defined]
        else:
            goal_handle.succeed()  # type: ignore[attr-defined]
        return result

    # -- публичные хуки для CLI/тестов (design §5.4 п.6 -- нет реального ASR/LLM) --

    def submit_confirm(self, *, is_yes: bool) -> None:
        """Передать да/нет-ответ на «Идём дальше?» активному туру, если он есть."""
        ctx = self._log_hook_call("submit_confirm", extra=f"is_yes={is_yes}")
        if ctx is not None:
            ctx.submit_confirm(is_yes=is_yes)

    def submit_answer(
        self, text: str, *, outcome: int = SubmitAnswer.Request.OUTCOME_RESUME_BASE
    ) -> None:
        """Передать ответ посетителя (и что с ним делать) активному туру, если он есть."""
        ctx = self._log_hook_call("submit_answer", extra=f"text={text!r} outcome={outcome}")
        if ctx is not None:
            ctx.submit_answer(text, outcome=outcome)

    def request_pause(self) -> None:
        """Запросить паузу тура (тестовый хук вместо /mission/presence, design §6)."""
        ctx = self._log_hook_call("request_pause")
        if ctx is not None:
            ctx.request_pause()

    def request_resume(self) -> None:
        """Запросить возобновление тура из PAUSED (тестовый хук)."""
        ctx = self._log_hook_call("request_resume")
        if ctx is not None:
            ctx.request_resume()

    def redirect(self, location_id: str) -> tuple[bool, str]:
        """«Отведи к X» во время тура (stage2 B2) -- зовётся и `~/redirect`, и тестами напрямую.

        Решает СИНХРОННО по текущему `/mission/state`, не дожидаясь
        FSM-потока -- тот заберёт `location_id` из очереди на ближайшем
        eligible-poll (fsm/base.py) и продолжит асинхронно, как и
        RunTour-цель в tool_broker._send_run_tour. `known_location_ids`
        валидируется здесь же, чтобы `NavigatingState` не упал на
        `KeyError` из `resolve_pose()` внутри FSM-потока.
        """
        with self._state_lock:
            current_state = (
                self._last_state_msg.state
                if self._last_state_msg is not None
                else MissionState.STATE_IDLE
            )
        with self._exec_lock:
            ctx = self._active_ctx
        if ctx is None or current_state not in _REDIRECT_ALLOWED_STATES:
            return False, "редирект сейчас недоступен"
        if location_id not in ctx.known_location_ids:
            return False, f"локация {location_id!r} не найдена"
        ctx.request_redirect(location_id)
        return True, "еду"

    def _log_hook_call(self, name: str, *, extra: str = "") -> FsmContext | None:
        with self._exec_lock:
            ctx = self._active_ctx
        suffix = f" ({extra})" if extra else ""
        if ctx is None:
            self.get_logger().warning(f"{name}{suffix}: нет активного тура, проигнорирован")
        else:
            self.get_logger().info(f"{name}{suffix}: доставлен активному туру")
        return ctx

    # -- ROS-обёртки над хуками выше, для mission_cli (design §11) -----------

    def _srv_request_pause(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        del request
        with self._exec_lock:
            has_ctx = self._active_ctx is not None
        self.request_pause()
        response.success = has_ctx
        response.message = "" if has_ctx else "нет активного тура"
        return response

    def _srv_request_resume(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        del request
        with self._exec_lock:
            has_ctx = self._active_ctx is not None
        self.request_resume()
        response.success = has_ctx
        response.message = "" if has_ctx else "нет активного тура"
        return response

    def _srv_submit_confirm(
        self, request: SetBool.Request, response: SetBool.Response
    ) -> SetBool.Response:
        with self._exec_lock:
            has_ctx = self._active_ctx is not None
        self.submit_confirm(is_yes=request.data)
        response.success = has_ctx
        response.message = "" if has_ctx else "нет активного тура"
        return response

    def _srv_submit_answer(
        self, request: SubmitAnswer.Request, response: SubmitAnswer.Response
    ) -> SubmitAnswer.Response:
        with self._exec_lock:
            has_ctx = self._active_ctx is not None
        self.submit_answer("", outcome=request.outcome)
        response.accepted = has_ctx
        response.message = "" if has_ctx else "нет активного тура"
        # Актуален в момент ANSWERING -- публикуется _on_fsm_state_changed
        # на каждом переходе, ЛЛМ полезно знать, куда именно вернётся тур.
        with self._state_lock:
            last = self._last_state_msg
            response.resume_token = last.resume_token if last is not None else ""
        return response

    def _srv_redirect(
        self, request: Redirect.Request, response: Redirect.Response
    ) -> Redirect.Response:
        response.accepted, response.message = self.redirect(str(request.location_id))
        return response

    # -- публикация /mission/state + RunTour feedback ------------------------

    def _on_fsm_state_changed(self, name: str, blackboard: Blackboard) -> None:
        msg = MissionState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.state = _STATE_ENUM.get(name, MissionState.STATE_IDLE)
        msg.interrupt = (
            MissionState.IRQ_ANSWERING if name == "answering" else MissionState.IRQ_NONE
        )
        # base_state -- "состояние под прерыванием (== state, если IRQ_NONE)"
        # (MissionState.msg): ANSWERING -- единственный интеррапт, который
        # сейчас различается от своего base (blackboard.interrupted_from,
        # см. root_sm.py); AWAITING_CONFIRM резюмируется в себя же
        # (fsm/states/awaiting_confirm.py) и потому не интеррапт с чужим base.
        msg.base_state = (
            _STATE_ENUM.get(blackboard.interrupted_from, msg.state)
            if msg.interrupt != MissionState.IRQ_NONE
            else msg.state
        )
        msg.tour_id = blackboard.tour.tour_id
        msg.stop_index = blackboard.tour.index
        msg.stop_total = len(blackboard.tour.stop_ids)
        msg.stop_id = blackboard.tour.current_stop_id
        msg.exhibit_id = blackboard.tour.current_exhibit_id
        msg.next_stop_id = blackboard.tour.next_stop_id
        msg.next_exhibit_id = blackboard.tour.next_exhibit_id
        msg.resume_token = blackboard.resume_token
        msg.resume_available = bool(blackboard.resume_token)
        # stage2 D3: hold_position -- единственный сейчас реальный источник
        # pause_reason (PAUSE_SAFETY/PAUSE_PRESENCE не заведены, см.
        # fsm/states/paused.py и recompute_safety_hold -- тот идёт через
        # отдельный HELD, не PausedState).
        if name == "paused" and blackboard.pause_reason == "user":
            msg.pause_reason = MissionState.PAUSE_USER
        self.get_logger().info(
            f"-> {name.upper()} (остановка {msg.stop_index + 1}/{msg.stop_total}, "
            f"stop_id={msg.stop_id or '-'}, exhibit_id={msg.exhibit_id or '-'}, "
            f"resume={'да' if msg.resume_available else 'нет'})"
        )
        with self._state_lock:
            self._last_state_msg = msg
            self._state_pub.publish(msg)

        with self._exec_lock:
            goal_handle = self._active_goal_handle
        if goal_handle is not None:
            feedback = RunTour.Feedback(
                phase=msg.state,
                stop_index=msg.stop_index,
                stop_total=msg.stop_total,
                stop_id=msg.stop_id,
            )
            goal_handle.publish_feedback(feedback)  # type: ignore[attr-defined]

    def _publish_heartbeat(self) -> None:
        with self._state_lock:
            last = self._last_state_msg
            if last is None:
                return
            msg = MissionState()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.state = last.state
            msg.interrupt = last.interrupt
            msg.base_state = last.base_state
            msg.tour_id = last.tour_id
            msg.stop_index = last.stop_index
            msg.stop_total = last.stop_total
            msg.stop_id = last.stop_id
            msg.exhibit_id = last.exhibit_id
            msg.next_stop_id = last.next_stop_id
            msg.next_exhibit_id = last.next_exhibit_id
            msg.resume_token = last.resume_token
            msg.resume_available = last.resume_available
            self._state_pub.publish(msg)


def main(args: list[str] | None = None) -> None:
    """Точка входа. FSM исполняется внутри execute_callback RunTour, отдельного потока нет."""
    rclpy.init(args=args)
    node = MissionFsmNode()
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
