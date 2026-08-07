"""NAVIGATING (design §5.2, §5.5): `NavigateToPose` на текущую остановку тура.

`nav_failed` (таймаут или ABORT) НЕ абортит тур (design §5.5) -- пропускает
остановку (`stops_skipped++`) и сразу продвигает `tour.index`, возвращая
либо `NAV_FAILED` (едем к следующей), либо `TOUR_FINISHED` (остановок
больше нет). Полноценный диалог "рассказать отсюда / пропустить" из §5.5
требует того же недостающего ASR/LLM-матчинга, что и AWAITING_CONFIRM --
в шаге 7 сознательно не реализован, дефолт `on_timeout=ASSUME_DEFAULT`
("пропустить") применяется напрямую, без вопроса.

Транзитный нарратив на ходу (design §5.6) отложен -- см. решение к шагу 7
в истории задачи: NAVIGATING только ведёт `NavigateToPose`, без
параллельного DROPPABLE-`Narrate`.
"""

from __future__ import annotations

from action_msgs.msg import GoalStatus
from nav2_msgs.action import NavigateToPose

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["NavigatingState"]


class NavigatingState(InterruptibleState):
    """Отправляет `NavigateToPose` на позу текущей остановки, следит за таймаутом."""

    name = "navigating"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить NavigateToPose на позу текущей остановки тура."""
        goal = NavigateToPose.Goal()
        goal.pose = self.ctx.resolve_pose(blackboard.tour.current_stop_id)
        self._send_future = self.ctx.nav_client.send_goal_async(goal)
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        self._start_ns = self.ctx.now_ns()

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться принятия goal-а, затем результата, следя за nav_stop_timeout_s."""
        if self._goal_handle is None:
            return self._poll_send(blackboard)
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s >= self.ctx.nav_stop_timeout_s:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
            reason = f"nav_stop_timeout_s={self.ctx.nav_stop_timeout_s} истёк"
            return self._skip_stop(blackboard, reason)
        return self._poll_result(blackboard)

    def _poll_send(self, blackboard: Blackboard) -> str | None:
        if not self._send_future.done():  # type: ignore[attr-defined]
            return None
        self._goal_handle = self._send_future.result()  # type: ignore[attr-defined]
        if not self._goal_handle.accepted:  # type: ignore[attr-defined]
            return self._skip_stop(blackboard, "NavigateToPose goal не принят")
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self, blackboard: Blackboard) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        status = self._result_future.result().status  # type: ignore[attr-defined]
        if status == GoalStatus.STATUS_SUCCEEDED:
            return outcomes.ARRIVED
        return self._skip_stop(blackboard, f"NavigateToPose status={status}")

    def _skip_stop(self, blackboard: Blackboard, reason: str) -> str:
        self.ctx.log(
            f"navigating: пропускаю остановку {blackboard.tour.current_stop_id!r} ({reason})"
        )
        blackboard.stops_skipped += 1
        if not blackboard.tour.has_next_stop:
            return outcomes.TOUR_FINISHED
        blackboard.tour.index += 1
        return outcomes.NAV_FAILED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD -- отменить активный NavigateToPose."""
        del blackboard, outcome
        if self._goal_handle is not None and self._result_future is not None:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
