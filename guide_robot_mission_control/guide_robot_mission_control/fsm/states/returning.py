"""RETURNING (design §5.2, §5.5): едет домой, если tour.return_home -- конец тура.

Последнее состояние прогона: любой её исход, кроме HELD, ведёт `root_sm`
к завершению `run_tour()` (design §5.2 явно не описывает "конец тура" --
им становится RETURNING). barge-in здесь, как и в NAVIGATING, не
отслеживается -- по той же причине (§5.6 отложен, отслеживать нечего).
"""

from __future__ import annotations

from action_msgs.msg import GoalStatus
from nav2_msgs.action import NavigateToPose

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["ReturningState"]


class ReturningState(InterruptibleState):
    """NavigateToPose на домашнюю позу, если это требуется планом тура."""

    name = "returning"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить NavigateToPose на домашнюю позу, если tour.return_home."""
        self._skip = not blackboard.tour.return_home
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        if self._skip:
            return
        goal = NavigateToPose.Goal()
        goal.pose = self.ctx.home_pose()
        self._send_future = self.ctx.nav_client.send_goal_async(goal)

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться результата возврата домой -- или сразу выйти, если return_home=False."""
        del blackboard, now_ns
        if self._skip:
            return outcomes.SUCCEEDED
        if self._goal_handle is None:
            return self._poll_send()
        return self._poll_result()

    def _poll_send(self) -> str | None:
        if not self._send_future.done():  # type: ignore[attr-defined]
            return None
        self._goal_handle = self._send_future.result()  # type: ignore[attr-defined]
        if not self._goal_handle.accepted:  # type: ignore[attr-defined]
            return outcomes.ABORTED
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        status = self._result_future.result().status  # type: ignore[attr-defined]
        if status == GoalStatus.STATUS_SUCCEEDED:
            return outcomes.SUCCEEDED
        return outcomes.ABORTED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD -- отменить активный NavigateToPose до дома."""
        del blackboard, outcome
        if self._goal_handle is not None and self._result_future is not None:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
