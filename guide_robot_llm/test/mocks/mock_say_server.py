"""Мок Say.action-сервера, заменяет tts_node в тестах.

Копия guide_robot_mission_control/test/mocks/mock_say_server.py -- см.
докстринг sim_clock.py про причину копии, не импорта.

ВАЖНО: этот мок гасит активную цель БЕЗУСЛОВНО любым CancelAll вне
зависимости от scope (bump epoch) -- это семантика реального tts_node
ДО фикса `guide_robot_llm/llm_plam.md` §1.4/§0.5 (guide_robot_voice/
tts_node.py:_on_cancel_all теперь сначала фильтрует по scope и трогает
физический вывод только если реально что-то сняли с активного слота).
Здесь оставлено как есть: ни один сценарий в тестах этого пакета не
зависит от разошедшегося поведения (barge-in здесь всегда совпадает по
scope с тем, что играет), а держать мок синхронизированным с обоими
поведениями сразу -- лишняя сложность без выгоды для этого захода.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll, SpeakingStatus

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

        self.fail_on_text: set[str] = set()
        self.delay_result_s: dict[str, float] = {}
        self.never_return_result: set[str] = set()

        self._lock = threading.Lock()
        self.epoch = 0
        self.goals_received = 0
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
        """rclpy.ok() для СВОЕГО контекста, не глобального дефолтного."""
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
        with self._lock:
            self.goals_received += 1
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
