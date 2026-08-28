"""REDIRECT_DONE (stage2 B2): финальная фраза после редиректа, затем конец тура.

Терминальное состояние одностопового плана редиректа
(`root_sm._apply_redirect`): один `Say(redirect_done_phrase, scope=dialog)`,
затем тур завершается SUCCEEDED -- БЕЗ `RETURNING` (design блок B: робот
остаётся на месте, `IDLE` там же, диалог продолжается благодаря грейсу
из A5). Итог `Say` (в т.ч. `STATUS_PREEMPTED` барж-ином) не меняет исход
-- к этому моменту редирект уже довёз посетителя до цели, а сама фраза --
не более чем вежливость.
"""

from __future__ import annotations

from guide_robot_msgs.action import Say

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["RedirectDoneState"]


class RedirectDoneState(InterruptibleState):
    """Говорит `redirect_done_phrase`, затем безусловно SUCCEEDED."""

    name = "redirect_done"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить прощальную фразу редиректа."""
        del blackboard
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        goal = Say.Goal(
            text=self.ctx.redirect_done_phrase,
            scope=Say.Goal.SCOPE_DIALOG,
            priority=Say.Goal.PRIORITY_DIALOG,
            interruptible=True,
        )
        self._send_future = self.ctx.say_client.send_goal_async(goal)

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться результата фразы -- исход `Say` тур не меняет, см. докстринг."""
        del blackboard, now_ns
        if self._goal_handle is None:
            return self._poll_send()
        return self._poll_result()

    def _poll_send(self) -> str | None:
        if not self._send_future.done():  # type: ignore[attr-defined]
            return None
        self._goal_handle = self._send_future.result()  # type: ignore[attr-defined]
        if not self._goal_handle.accepted:  # type: ignore[attr-defined]
            return outcomes.SUCCEEDED
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        return outcomes.SUCCEEDED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD -- отменить активную прощальную фразу."""
        del blackboard, outcome
        if self._goal_handle is not None and self._result_future is not None:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
