"""GREETING (design §5.2, §5.5): один `Say(приветствие, scope=dialog)`, если `tour.greet`.

Барж-ин ловится по результату `Say` (`STATUS_PREEMPTED`) -- та же логика,
что и epoch-фенсинг в `guide_robot_voice`/`narration_server`: любой Say-goal,
не только `Narrate`-чанки, гасится `CancelAll` одинаково.
"""

from __future__ import annotations

from guide_robot_msgs.action import Say

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["GreetingState"]


class GreetingState(InterruptibleState):
    """Здоровается один раз, если tour.greet -- иначе сразу SUCCEEDED."""

    name = "greeting"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить приветственный Say, если это требуется планом тура."""
        self._skip = not blackboard.tour.greet
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        if self._skip:
            return
        goal = Say.Goal(
            text=self.ctx.greeting_text,
            scope=Say.Goal.SCOPE_DIALOG,
            priority=Say.Goal.PRIORITY_DIALOG,
            interruptible=True,
        )
        self._send_future = self.ctx.say_client.send_goal_async(goal)

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться результата приветствия -- или сразу выйти, если greet=False."""
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
            return outcomes.SUCCEEDED  # не смогли поздороваться -- тур это не блокирует
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        result: Say.Result = self._result_future.result().result  # type: ignore[attr-defined]
        if result.status == Say.Result.STATUS_PREEMPTED:
            return outcomes.INTERRUPTED
        return outcomes.SUCCEEDED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD -- отменить активный Say приветствия."""
        del blackboard, outcome
        if self._goal_handle is not None and self._result_future is not None:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
