"""Мок Say.action-сервера, заменяет tts_node в тестах (design §9.1, §0.5).

Не реализует полный планировщик приоритетов tts_node
(guide_robot_voice/lib/scheduler.py): narration_server никогда не держит
больше одной активной плюс одной ожидающей (lookahead<=1) цели
одновременно, приоритетная конкуренция между несколькими независимыми
клиентами Say вне рамок тестов этого пакета. Одна очередь FIFO, один
активный goal -- этого достаточно.

Что мок обязан воспроизвести точно -- это семантика CancelAll реального
tts_node (tts_node.py:_on_cancel_all): активная цель гасится БЕЗУСЛОВНО
любым CancelAll вне зависимости от scope (bump epoch), а ещё не начавшие
звучать (в очереди) цели снимаются, только если msg.scope совпадает с их
scope либо msg.scope == SCOPE_ALL. Без этого не проверить ключевой
инвариант narration_resume: после MODE_HARD ни один чанк не проигрывается
в мёртвый epoch.

Скорость озвучки считается по self.get_clock() -- узел обязан подниматься
с use_sim_time:=true и получать /clock от sim_clock.py, иначе тесты на
120-секундные таймауты не смогут прокручивать время мгновенно.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import rclpy
from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll, SpeakingStatus
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = ["MockSayServer"]

_QOS_CANCEL_ALL = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
_QOS_SPEAKING = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

_POLL_S = 0.001


@dataclass
class _QueuedGoal:
    goal_id: str
    scope: int


class MockSayServer(Node):
    """Say.action на виртуальных часах, с epoch-fencing и инъекцией отказов."""

    def __init__(self, node_name: str = "mock_say_server", **node_kwargs: object) -> None:
        """Поднять action-сервер `say`, подписку /speech/cancel_all и паблишер /voice/speaking."""
        super().__init__(node_name, **node_kwargs)
        self.declare_parameter("chars_per_sec", 15.0)
        self.chars_per_sec = float(self.get_parameter("chars_per_sec").value)

        # Инъекция отказов -- ключ: request.text (goal_id сервер назначает
        # сам, тест не может знать его заранее; текст чанка тест знает,
        # потому что сам же его и положил в фикстуру mock_semantic_map).
        self.fail_on_text: set[str] = set()
        self.delay_result_s: dict[str, float] = {}
        self.never_return_result: set[str] = set()

        self._lock = threading.Lock()
        self.epoch = 0
        self._active_goal_id: str | None = None
        self._queue: list[_QueuedGoal] = []
        self._preempted_goal_ids: set[str] = set()

        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            _QOS_CANCEL_ALL,
            callback_group=MutuallyExclusiveCallbackGroup(),
        )
        self._status_pub = self.create_publisher(SpeakingStatus, "/voice/speaking", _QOS_SPEAKING)
        self._action_server = ActionServer(
            self,
            Say,
            "say",
            execute_callback=self._execute,
            goal_callback=self._on_goal,
            cancel_callback=lambda handle: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )

    def _ok(self) -> bool:
        """rclpy.ok(), но для СВОЕГО контекста.

        Голый rclpy.ok() без аргумента проверяет глобальный дефолтный
        контекст. harness.py поднимает каждый тест на отдельном
        rclpy.Context() (design §9.1 -- изоляция тестов друг от друга), а
        дефолтный контекст в процессе pytest никогда не инициализируется.
        Голый rclpy.ok() в этом случае всегда False -- цикл вида
        `while self._ok():` не выполнится ни разу, и любой поллинг здесь
        молча завершится, как будто нода уже остановлена.
        """
        return rclpy.ok(context=self.context)

    # -- CancelAll --------------------------------------------------------

    def _on_cancel_all(self, msg: CancelAll) -> None:
        with self._lock:
            self.epoch += 1
            survivors = []
            dropped: list[_QueuedGoal] = []
            for queued in self._queue:
                if msg.scope in (CancelAll.SCOPE_ALL, queued.scope):
                    dropped.append(queued)
                else:
                    survivors.append(queued)
            self._queue = survivors
        self._preempted_goal_ids.update(g.goal_id for g in dropped)

    # -- приём/выполнение ---------------------------------------------------

    def _on_goal(self, goal_request: Say.Goal) -> GoalResponse:
        if not goal_request.text.strip():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute(self, goal_handle: object) -> Say.Result:
        request: Say.Goal = goal_handle.request  # type: ignore[attr-defined]
        goal_id = bytes(goal_handle.goal_id.uuid).hex()  # type: ignore[attr-defined]

        with self._lock:
            self._queue.append(_QueuedGoal(goal_id=goal_id, scope=int(request.scope)))

        if not self._wait_for_turn(goal_id, goal_handle):
            return self._finish_preempted(goal_handle, "cancel_all")

        # Эпоха фиксируется в момент, когда цель РЕАЛЬНО начинает звучать
        # (как в tts_node._speak: `epoch = self._sink.epoch`), не в момент
        # постановки в очередь. Иначе цель, пережившая scope-фильтр
        # _on_cancel_all и получившая слот только после чужого CancelAll,
        # сравнивала бы себя с уже устаревшим epoch и считала бы себя
        # преёмнутой, хотя её вообще не отменяли.
        with self._lock:
            epoch_at_start = self.epoch
        return self._speak(goal_handle, request, goal_id, epoch_at_start)

    def _wait_for_turn(self, goal_id: str, goal_handle: object) -> bool:
        """Дождаться, пока goal_id встанет в голову очереди и слот свободен."""
        while self._ok():
            if goal_id in self._preempted_goal_ids:
                self._preempted_goal_ids.discard(goal_id)
                return False
            with self._lock:
                if (
                    self._active_goal_id is None
                    and self._queue
                    and self._queue[0].goal_id == goal_id
                ):
                    self._active_goal_id = goal_id
                    self._queue.pop(0)
                    return True
            if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                with self._lock:
                    self._queue = [g for g in self._queue if g.goal_id != goal_id]
                return False
            time.sleep(_POLL_S)
        return False

    def _finish_preempted(self, goal_handle: object, message: str) -> Say.Result:
        result = Say.Result(status=Say.Result.STATUS_PREEMPTED, message=message)
        goal_handle.abort()  # type: ignore[attr-defined]
        return result

    def _speak(
        self, goal_handle: object, request: Say.Goal, goal_id: str, epoch_at_submit: int
    ) -> Say.Result:
        text = request.text
        total_chars = len(text)
        start = self.get_clock().now()
        self._publish_status(
            speaking=True, goal_id=goal_id, priority=request.priority, scope=request.scope
        )

        result = self._run_fault_or_pace(goal_handle, text, total_chars, start, epoch_at_submit)

        if result.status == Say.Result.STATUS_COMPLETED:
            goal_handle.succeed()  # type: ignore[attr-defined]
        elif result.status == Say.Result.STATUS_CANCELLED:
            goal_handle.canceled()  # type: ignore[attr-defined]
        else:
            goal_handle.abort()  # type: ignore[attr-defined]

        with self._lock:
            self._active_goal_id = None
        self._publish_status(speaking=False, goal_id="", priority=0, scope=0)
        return result

    def _run_fault_or_pace(
        self, goal_handle: object, text: str, total_chars: int, start: object, epoch_at_submit: int
    ) -> Say.Result:
        if text in self.fail_on_text:
            return Say.Result(status=Say.Result.STATUS_FAILED, message="injected_failure")

        if text in self.never_return_result:
            # Симулирует зависший бэкенд: сервер никогда не пришлёт результат.
            # Выход только через остановку rclpy (teardown теста) -- значение
            # ниже недостижимо в штатной работе теста, только на shutdown.
            while self._ok():
                time.sleep(_POLL_S)
            return Say.Result(status=Say.Result.STATUS_FAILED, message="never_return_result")

        delay = self.delay_result_s.get(text)
        if delay:
            self._sleep_sim(delay)

        return self._pace(goal_handle, text, total_chars, start, epoch_at_submit)

    def _pace(
        self, goal_handle: object, text: str, total_chars: int, start: object, epoch_at_submit: int
    ) -> Say.Result:
        status = Say.Result.STATUS_COMPLETED
        message = ""
        spoken_chars = 0
        while True:
            elapsed = (self.get_clock().now() - start).nanoseconds / 1e9
            spoken_chars = min(total_chars, int(elapsed * self.chars_per_sec))
            # Мок не режет текст на клаузы (§0.5) -- одна псевдо-клауза на
            # весь text, только чтобы progress был наблюдаем извне (тесты
            # синхронизируются по нему, не по реальному времени).
            goal_handle.publish_feedback(  # type: ignore[attr-defined]
                Say.Feedback(
                    clause_index=0,
                    clause_count=1,
                    progress=spoken_chars / max(1, total_chars),
                    current_clause=text,
                )
            )

            with self._lock:
                current_epoch = self.epoch
            if current_epoch != epoch_at_submit:
                status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"
                break
            if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                status, message = Say.Result.STATUS_CANCELLED, "goal_cancel"
                break
            if spoken_chars >= total_chars:
                spoken_chars = total_chars
                break
            time.sleep(_POLL_S)

        return Say.Result(
            status=status,
            spoken_text=text[:spoken_chars],
            spoken_chars=spoken_chars,
            spoken_duration=(self.get_clock().now() - start).nanoseconds / 1e9,
            message=message,
        )

    def _sleep_sim(self, seconds: float) -> None:
        start = self.get_clock().now()
        target_ns = int(seconds * 1e9)
        while self._ok() and (self.get_clock().now() - start).nanoseconds < target_ns:
            time.sleep(_POLL_S)

    def _publish_status(self, *, speaking: bool, goal_id: str, priority: int, scope: int) -> None:
        msg = SpeakingStatus()
        msg.stamp = self.get_clock().now().to_msg()
        msg.speaking = speaking
        msg.epoch = self.epoch
        msg.goal_id = goal_id
        msg.priority = priority
        msg.scope = scope
        self._status_pub.publish(msg)
