"""PAUSED (design §5.2, §6): тур приостановлен, пока рядом нет посетителя.

Живой сигнал -- подписка `mission_fsm` на `/mission/presence`
(`presence_monitor`, design §6) -- отложен вместе с остальной интеграцией
присутствия в состояния тура; в v1 вход/выход из PAUSED идёт через
тестовые хуки `FsmContext.request_pause()`/`request_resume()`
(`NARRATING` вызывает `take_pause_request()` -- см. fsm/states/narrating.py).

Не трогает стек прерываний -- как и HELD, не фрейм (design §5.4 не
относит PAUSED к правилам стека вообще).
"""

from __future__ import annotations

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["PausedState"]


class PausedState(InterruptibleState):
    """Ждёт request_resume() или pause_timeout_s ("никого нет -- едем домой")."""

    name = "paused"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Запомнить момент входа -- отсчёт `pause_timeout_s` идёт от него."""
        del blackboard
        self._start_ns = self.ctx.now_ns()

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """RESUMED по тестовому хуку; TIMEOUT_NO_VISITOR по истечении pause_timeout_s."""
        del blackboard
        if self.ctx.take_resume_request():
            return outcomes.RESUMED
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s >= self.ctx.pause_timeout_s:
            return outcomes.TIMEOUT_NO_VISITOR
        return None
