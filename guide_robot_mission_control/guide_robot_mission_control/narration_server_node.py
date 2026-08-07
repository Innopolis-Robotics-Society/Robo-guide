"""narration_server -- озвучивает один экспозиционный текст по чанкам Say.action.

design §4, реконсиляция §0.5. Не режет текст сам: один элемент
`GetExhibitContent.chunks` -- один `Say` goal (§0.5, "Чанкует не
narration_server"). Отвечает за: конвейер с lookahead (§4.2), мягкую паузу
(`NarrationControl.MODE_SOFT`) и жёсткую остановку (`MODE_HARD` / barge-in /
cancel), учёт произнесённого через `chunk_plan.ChunkPlan` и вычисление
`resume_token` через `resume.py`.

"started" чанка (для запуска lookahead) детектируется по первому feedback
от Say, а не по отдельной подписке на `SpeakingStatus` с корреляцией по
goal_id (§0.5 admissible-альтернатива): мок и реальный `tts_node` шлют
feedback сразу при входе в синтез первой клаузы, так что первый feedback
уже эквивалентен "started" и не требует второго канала.

Повторные Narrate-вызовы на один и тот же exhibit_id/version (пауза/резюме
в рамках одной активной сессии) должны видеть накопленный набор чанков,
признанных потерянными при resume_policy=continue_next -- иначе
`ChunkPlan` при пересборке пометит их DONE и припишет им текст, которого
никто не говорил (design §3.3, инвариант). Грамматика `resume_token`
(v1|exhibit_id|version|chunk_idx|char_off) такого набора не несёт, поэтому
`self._skipped_by_exhibit` держит его в памяти узла на время жизни сессии;
это осознанное расширение контракта, не описанное в design буквально.
"""

from __future__ import annotations

import functools
import hashlib
import threading
import time
from dataclasses import dataclass, field

import rclpy
from guide_robot_msgs.action import Narrate, Say
from guide_robot_msgs.msg import CancelAll
from guide_robot_msgs.srv import GetExhibitContent, NarrationControl
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future

from guide_robot_mission_control.chunk_plan import ChunkPlan, ChunkState
from guide_robot_mission_control.lib.qos import QOS_CANCEL_ALL
from guide_robot_mission_control.resume import (
    ResumeOutcome,
    ResumePolicy,
    ResumeToken,
    TokenError,
    apply_resume_policy,
    resolve_resume,
)

__all__ = ["NarrationServerNode", "main"]

_POLL_S = 0.001


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


@dataclass
class _PendingChunk:
    """Чанк в полёте: goal Say отправлен, результат ещё не пришёл."""

    handle: object | None = None
    last_progress: float = 0.0


@dataclass
class _ActiveExecution:
    """Состояние одного исполняемого Narrate goal.

    Разделяется между потоком execute_callback (единственный писатель
    ChunkPlan.mark по PENDING/SENT-переходам) и колбэками результатов/
    feedback Say, NarrationControl-сервисом и подпиской на CancelAll --
    все три читают/просят остановку из других потоков executor-а. `lock`
    защищает `plan` и `pending`; `hard_event`/`soft_event` -- сигналы
    останова, `finished_event` -- сигнал "goal_handle уже завершён",
    на который ждут NarrationControl и on_deactivate.
    """

    goal_handle: object
    plan: ChunkPlan
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[int, _PendingChunk] = field(default_factory=dict)
    hard_event: threading.Event = field(default_factory=threading.Event)
    hard_reason: str = ""
    soft_event: threading.Event = field(default_factory=threading.Event)
    finished_event: threading.Event = field(default_factory=threading.Event)


class NarrationServerNode(LifecycleNode):
    """Lifecycle-нода: `Narrate` action-сервер + `NarrationControl` сервис."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Ресурсы ROS захватываются в on_configure."""
        super().__init__("narration_server", **node_kwargs)

        self.declare_parameter("lookahead", 1)
        self.declare_parameter("resume_policy", "repeat_chunk")
        self.declare_parameter("resume_bridge_enabled", True)
        self.declare_parameter("resume_bridge_text", "Продолжаю.")
        self.declare_parameter("soft_pause_max_s", 8.0)
        self.declare_parameter("hard_stop_result_timeout_s", 0.3)
        self.declare_parameter("say_priority", int(Say.Goal.PRIORITY_NARRATION))
        self.declare_parameter("say_scope", int(Say.Goal.SCOPE_NARRATION))
        self.declare_parameter("content_language", "ru")
        self.declare_parameter("content_mode", "full")
        self.declare_parameter("service_call_timeout_s", 2.0)

        self._active = False
        self._lookahead = 1
        self._resume_policy = "repeat_chunk"
        self._resume_bridge_enabled = True
        self._resume_bridge_text = ""
        self._soft_pause_max_s = 8.0
        self._hard_stop_result_timeout_s = 0.3
        self._say_priority = 0
        self._say_scope = 0
        self._content_language = "ru"
        self._content_mode = "full"
        self._service_call_timeout_s = 2.0

        self._skipped_by_exhibit: dict[str, set[int]] = {}
        self._exec_lock = threading.Lock()
        self._active_execution: _ActiveExecution | None = None

        self._cb_reentrant = ReentrantCallbackGroup()
        self._cb_sub = MutuallyExclusiveCallbackGroup()

    def _ok(self) -> bool:
        """rclpy.ok() для СВОЕГО контекста -- см. test/mocks/mock_say_server.py:_ok()."""
        return rclpy.ok(context=self.context)

    # -- lifecycle ----------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять клиентов/сервер/подписки."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._lookahead = int(self.get_parameter("lookahead").value)
        self._resume_policy = str(self.get_parameter("resume_policy").value)
        ResumePolicy(self._resume_policy)  # рано провалить configure на битом значении
        self._resume_bridge_enabled = bool(self.get_parameter("resume_bridge_enabled").value)
        self._resume_bridge_text = str(self.get_parameter("resume_bridge_text").value)
        self._soft_pause_max_s = float(self.get_parameter("soft_pause_max_s").value)
        self._hard_stop_result_timeout_s = float(
            self.get_parameter("hard_stop_result_timeout_s").value
        )
        self._say_priority = int(self.get_parameter("say_priority").value)
        self._say_scope = int(self.get_parameter("say_scope").value)
        self._content_language = str(self.get_parameter("content_language").value)
        self._content_mode = str(self.get_parameter("content_mode").value)
        self._service_call_timeout_s = float(self.get_parameter("service_call_timeout_s").value)

        self._content_client = self.create_client(
            GetExhibitContent, "/content_server/get_exhibit_content"
        )
        self._say_client = ActionClient(self, Say, "say", callback_group=self._cb_reentrant)

        self._cancel_pub = self.create_lifecycle_publisher(
            CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL
        )
        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_sub,
        )
        self._narrate_server = ActionServer(
            self,
            Narrate,
            "narrate",
            execute_callback=self._execute_narrate,
            goal_callback=self._on_narrate_goal,
            cancel_callback=lambda handle: CancelResponse.ACCEPT,
            callback_group=self._cb_reentrant,
        )
        self._control_srv = self.create_service(
            NarrationControl,
            "~/control",
            self._handle_control,
            callback_group=self._cb_sub,
        )

        self.get_logger().info(
            f"narration_server сконфигурирован: lookahead={self._lookahead}, "
            f"resume_policy={self._resume_policy}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Разрешить приём Narrate goal-ов."""
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Жёстко остановить активный нарратив (если есть) и запретить новые goal-ы.

        Ждём завершения execute_callback ограниченное время -- деактивация
        обязана уложиться в секунды (design §10), а не виснуть на потоке
        goal-а, если что-то пошло не так.
        """
        self._active = False
        self._request_hard_stop("deactivate")
        with self._exec_lock:
            ctx = self._active_execution
        if ctx is not None:
            ctx.finished_event.wait(timeout=self._hard_stop_result_timeout_s + 1.0)
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить накопленную сессионную память между sessions конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup -- отдельного пути завершения активного goal-а не требуется."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        self._skipped_by_exhibit.clear()

    # -- Narrate: приём и диспетчер -----------------------------------------

    def _on_narrate_goal(self, goal_request: Narrate.Goal) -> GoalResponse:
        if not self._active:
            return GoalResponse.REJECT
        if not goal_request.exhibit_id.strip():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute_narrate(self, goal_handle: object) -> Narrate.Result:
        goal: Narrate.Goal = goal_handle.request  # type: ignore[attr-defined]

        with self._exec_lock:
            busy = self._active_execution is not None
        if busy:
            return self._finish_narrate(
                goal_handle, Narrate.Result.OUTCOME_REJECTED, "", 0, 0, "", "busy"
            )

        chunks, version = self._resolve_content(goal)
        if not chunks:
            return self._finish_narrate(
                goal_handle,
                Narrate.Result.OUTCOME_REJECTED,
                "",
                0,
                0,
                "",
                "exhibit_not_found",
            )

        try:
            token = ResumeToken.parse(goal.resume_token)
        except TokenError as error:
            return self._finish_narrate(
                goal_handle,
                Narrate.Result.OUTCOME_REJECTED,
                "",
                0,
                len(chunks),
                "",
                str(error),
            )

        decision = resolve_resume(
            token, exhibit_id=goal.exhibit_id, version=version, chunk_count=len(chunks)
        )
        if decision.outcome is ResumeOutcome.REJECTED:
            return self._finish_narrate(
                goal_handle,
                Narrate.Result.OUTCOME_REJECTED,
                goal.resume_token,
                0,
                len(chunks),
                "",
                decision.detail,
            )

        policy = ResumePolicy(self._resume_policy)
        decision = apply_resume_policy(decision, policy, chunk_count=len(chunks))

        if decision.outcome is ResumeOutcome.ALREADY_COMPLETE:
            finished_token = ResumeToken(goal.exhibit_id, version, len(chunks), 0).format()
            return self._finish_narrate(
                goal_handle,
                Narrate.Result.OUTCOME_COMPLETED,
                finished_token,
                len(chunks),
                len(chunks),
                "",
                decision.detail,
            )

        skipped = self._skipped_by_exhibit.get(goal.exhibit_id, set())
        if decision.outcome is ResumeOutcome.START:
            skipped = set()
            self._skipped_by_exhibit.pop(goal.exhibit_id, None)
        if decision.skipped_chunk_idx is not None:
            skipped = skipped | {decision.skipped_chunk_idx}
            self._skipped_by_exhibit[goal.exhibit_id] = skipped

        plan = ChunkPlan(
            goal.exhibit_id,
            version,
            chunks,
            start_idx=decision.start_chunk_idx,
            skipped=skipped,
            lookahead=self._lookahead,
        )
        ctx = _ActiveExecution(goal_handle, plan)
        with self._exec_lock:
            self._active_execution = ctx

        try:
            if decision.outcome is ResumeOutcome.RESUME and plan.chunks_spoken() > 0:
                self._speak_resume_bridge()
            outcome, detail = self._run_plan(ctx)
        finally:
            with self._exec_lock:
                self._active_execution = None

        if outcome == Narrate.Result.OUTCOME_COMPLETED:
            self._skipped_by_exhibit.pop(goal.exhibit_id, None)

        result = self._finish_narrate(
            goal_handle,
            outcome,
            plan.resume_token(),
            plan.chunks_spoken(),
            plan.chunk_total,
            plan.spoken_text(),
            detail,
        )
        ctx.finished_event.set()
        return result

    def _finish_narrate(  # noqa: PLR0913 -- поля одного Result, не команда с побочными эффектами
        self,
        goal_handle: object,
        outcome: int,
        resume_token: str,
        chunks_spoken: int,
        chunks_total: int,
        spoken_text: str,
        detail: str,
    ) -> Narrate.Result:
        result = Narrate.Result(
            outcome=outcome,
            resume_token=resume_token,
            chunks_spoken=chunks_spoken,
            chunks_total=chunks_total,
            spoken_text=spoken_text,
            detail=detail,
        )
        if outcome in (Narrate.Result.OUTCOME_ABORTED, Narrate.Result.OUTCOME_REJECTED):
            goal_handle.abort()  # type: ignore[attr-defined]
        elif goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
            goal_handle.canceled()  # type: ignore[attr-defined]
        else:
            goal_handle.succeed()  # type: ignore[attr-defined]
        return result

    def _resolve_content(self, goal: Narrate.Goal) -> tuple[list[str], str]:
        """Взять готовый текст из goal.text, либо дёрнуть GetExhibitContent."""
        if goal.text.strip():
            version = hashlib.sha1(goal.text.encode("utf-8")).hexdigest()[:8]
            return [goal.text], version
        if not self._content_client.wait_for_service(timeout_sec=self._service_call_timeout_s):
            return [], ""
        future = self._content_client.call_async(
            GetExhibitContent.Request(
                exhibit_id=goal.exhibit_id,
                mode=self._content_mode,
                language=self._content_language,
            )
        )
        if not _wait_future(future, self.context, self._service_call_timeout_s):
            return [], ""
        response = future.result()
        return list(response.chunks), response.version

    def _speak_resume_bridge(self) -> None:
        """Короткая мостовая фраза перед возобновлением (design §3.2). Best-effort."""
        if not self._resume_bridge_enabled or not self._resume_bridge_text:
            return
        say_goal = Say.Goal(
            text=self._resume_bridge_text,
            priority=self._say_priority,
            scope=self._say_scope,
            interruptible=True,
        )
        send_future = self._say_client.send_goal_async(say_goal)
        if not _wait_future(send_future, self.context, self._hard_stop_result_timeout_s + 1.0):
            self.get_logger().warning("resume-мостик: Say goal не принят вовремя, пропускаю")
            return
        goal_handle = send_future.result()
        if not goal_handle.accepted:
            return
        result_future = goal_handle.get_result_async()
        _wait_future(result_future, self.context, self._soft_pause_max_s)

    # -- конвейер отправки чанков --------------------------------------------

    def _run_plan(self, ctx: _ActiveExecution) -> tuple[int, str]:
        while self._ok():
            if ctx.goal_handle.is_cancel_requested and not ctx.hard_event.is_set():  # type: ignore[attr-defined]
                ctx.hard_reason = "client_cancel"
                ctx.hard_event.set()
            if ctx.hard_event.is_set():
                return self._do_hard_stop(ctx)
            if ctx.soft_event.is_set():
                return self._do_soft_pause(ctx)
            with ctx.lock:
                complete = ctx.plan.is_complete()
            if complete:
                return Narrate.Result.OUTCOME_COMPLETED, ""
            with ctx.lock:
                idx = ctx.plan.next_to_send()
            if idx is not None:
                self._send_chunk(ctx, idx)
            time.sleep(_POLL_S)
        return Narrate.Result.OUTCOME_ABORTED, "node_shutdown"

    def _send_chunk(self, ctx: _ActiveExecution, idx: int) -> None:
        with ctx.lock:
            text = ctx.plan.chunk_text(idx)
            ctx.plan.mark(idx, ChunkState.SENT)
            ctx.pending[idx] = _PendingChunk()
        say_goal = Say.Goal(
            text=text, priority=self._say_priority, scope=self._say_scope, interruptible=True
        )
        send_future = self._say_client.send_goal_async(
            say_goal, feedback_callback=functools.partial(self._on_say_feedback, ctx, idx)
        )
        send_future.add_done_callback(functools.partial(self._on_say_goal_response, ctx, idx))

    def _on_say_feedback(self, ctx: _ActiveExecution, idx: int, feedback: object) -> None:
        progress = feedback.feedback.progress  # type: ignore[attr-defined]
        with ctx.lock:
            pending = ctx.pending.get(idx)
            if pending is None:
                return
            pending.last_progress = progress
            # Первый feedback уже означает "начал звучать" -- см. докстринг
            # модуля: отдельная подписка на SpeakingStatus не нужна.
            if ctx.plan.state_of(idx) == ChunkState.SENT:
                ctx.plan.mark(idx, ChunkState.SPEAKING)

    def _on_say_goal_response(self, ctx: _ActiveExecution, idx: int, future: Future) -> None:
        goal_handle = future.result()
        with ctx.lock:
            pending = ctx.pending.get(idx)
            if pending is None:
                return
            if not goal_handle.accepted:  # type: ignore[attr-defined]
                del ctx.pending[idx]
                ctx.plan.mark(idx, ChunkState.CUT, spoken_chars=0)
                return
            pending.handle = goal_handle
        result_future = goal_handle.get_result_async()  # type: ignore[attr-defined]
        result_future.add_done_callback(functools.partial(self._on_say_result, ctx, idx))

    def _on_say_result(self, ctx: _ActiveExecution, idx: int, future: Future) -> None:
        response = future.result()
        result: Say.Result = response.result  # type: ignore[attr-defined]
        with ctx.lock:
            if idx not in ctx.pending:
                return  # уже закрыт таймаутом hard-stop -- поздний колбэк, игнорируем
            del ctx.pending[idx]
            if result.status == Say.Result.STATUS_COMPLETED:
                ctx.plan.mark(idx, ChunkState.DONE)
            else:
                ctx.plan.mark(idx, ChunkState.CUT, spoken_chars=result.spoken_chars)

    # -- остановка: мягкая и жёсткая -----------------------------------------

    def _do_hard_stop(self, ctx: _ActiveExecution) -> tuple[int, str]:
        """CancelAll (если мы не реагируем на чужой) + отмена всех Say в полёте.

        `hard_stop_result_timeout_s` -- верхняя граница ожидания результатов;
        то, что не успело закрыться результатом, закрывается по последнему
        известному feedback.progress (design §4.2).
        """
        if ctx.hard_reason != "barge_in":
            msg = CancelAll(scope=self._say_scope, reason=CancelAll.REASON_OPERATOR)
            msg.stamp = self.get_clock().now().to_msg()
            self._cancel_pub.publish(msg)

        with ctx.lock:
            handles = [p.handle for p in ctx.pending.values() if p.handle is not None]
        for handle in handles:
            handle.cancel_goal_async()  # type: ignore[attr-defined]

        start = self.get_clock().now()
        while self._ok():
            with ctx.lock:
                still_pending = bool(ctx.pending)
            if not still_pending:
                break
            elapsed = (self.get_clock().now() - start).nanoseconds / 1e9
            if elapsed >= self._hard_stop_result_timeout_s:
                break
            time.sleep(_POLL_S)

        with ctx.lock:
            leftover = list(ctx.pending.items())
            ctx.pending.clear()
            for idx, pending in leftover:
                fallback_chars = int(pending.last_progress * len(ctx.plan.chunk_text(idx)))
                ctx.plan.mark(idx, ChunkState.CUT, spoken_chars=fallback_chars)

        return Narrate.Result.OUTCOME_INTERRUPTED, ctx.hard_reason or "hard_stop"

    def _do_soft_pause(self, ctx: _ActiveExecution) -> tuple[int, str]:
        """Не слать новых чанков; ждать SENT/SPEAKING до soft_pause_max_s, иначе эскалировать."""
        start = self.get_clock().now()
        while self._ok():
            with ctx.lock:
                inflight = bool(ctx.pending)
            if not inflight:
                return Narrate.Result.OUTCOME_PAUSED, ""
            if ctx.goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                ctx.hard_reason = "client_cancel"
                ctx.hard_event.set()
                return self._do_hard_stop(ctx)
            elapsed = (self.get_clock().now() - start).nanoseconds / 1e9
            if elapsed >= self._soft_pause_max_s:
                ctx.hard_reason = "soft_pause_escalation"
                ctx.hard_event.set()
                return self._do_hard_stop(ctx)
            time.sleep(_POLL_S)
        return Narrate.Result.OUTCOME_ABORTED, "node_shutdown"

    # -- внешние триггеры остановки ------------------------------------------

    def _request_hard_stop(self, reason: str) -> None:
        with self._exec_lock:
            ctx = self._active_execution
        if ctx is None:
            return
        if not ctx.hard_event.is_set():
            ctx.hard_reason = reason
        ctx.hard_event.set()

    def _request_soft_pause(self) -> None:
        with self._exec_lock:
            ctx = self._active_execution
        if ctx is None:
            return
        ctx.soft_event.set()

    def _on_cancel_all(self, msg: CancelAll) -> None:
        """Барж-ин слушаем сами, не через FSM (design §4.2, реконсиляция §0.5)."""
        if not self._active:
            return
        if msg.reason != CancelAll.REASON_BARGE_IN:
            return
        if msg.scope not in (CancelAll.SCOPE_ALL, self._say_scope):
            return
        self._request_hard_stop("barge_in")

    def _handle_control(
        self, request: NarrationControl.Request, response: NarrationControl.Response
    ) -> NarrationControl.Response:
        with self._exec_lock:
            ctx = self._active_execution
        if ctx is None:
            response.ok = False
            response.resume_token = ""
            response.chunks_spoken = 0
            return response

        if request.mode == NarrationControl.Request.MODE_SOFT:
            self._request_soft_pause()
        else:
            self._request_hard_stop("control")

        wait_timeout = self._soft_pause_max_s + self._hard_stop_result_timeout_s + 1.0
        finished = ctx.finished_event.wait(timeout=wait_timeout)
        response.ok = finished
        with ctx.lock:
            response.resume_token = ctx.plan.resume_token()
            response.chunks_spoken = ctx.plan.chunks_spoken()
        return response


def main(args: list[str] | None = None) -> None:
    """Точка входа. MultiThreadedExecutor -- goal Narrate и колбэки Say делят потоки."""
    rclpy.init(args=args)
    node = NarrationServerNode()
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
