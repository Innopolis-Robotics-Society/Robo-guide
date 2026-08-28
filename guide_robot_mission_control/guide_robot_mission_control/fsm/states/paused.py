"""PAUSED (design §5.2, §6): тур приостановлен, пока рядом нет посетителя.

Живой сигнал -- подписка `mission_fsm` на `/mission/presence`
(`presence_monitor`, design §6) -- отложен вместе с остальной интеграцией
присутствия в состояния тура; в v1 вход/выход из PAUSED идёт через
тестовые хуки `FsmContext.request_pause()`/`request_resume()`
(`NARRATING` вызывает `take_pause_request()` -- см. fsm/states/narrating.py).

Не трогает стек прерываний -- как и HELD, не фрейм (design §5.4 не
относит PAUSED к правилам стека вообще).

TODO(stage2 A6): "уход посетителя дольше таймаута -> RETURNING" уже есть
как ФОРМА (TIMEOUT_NO_VISITOR ниже), но без живой интеграции с
`/mission/presence` он не наступает сам по себе -- только через
`request_resume()`/тестовый хук или явную отмену. Once `/mission/presence`
здесь заведён, это станет единственным настоящим "посетитель ушёл"-путём
в RETURNING (отдельным от CANCELED посетителя-по-инициативе, который
теперь остаётся на месте, см. `fsm/root_sm.py::_UNIVERSAL`).
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
            self.ctx.log("paused: resume получен -> продолжаем")
            return outcomes.RESUMED
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s >= self.ctx.pause_timeout_s:
            self.ctx.log(f"paused: pause_timeout_s={self.ctx.pause_timeout_s} истёк -> едем домой")
            return outcomes.TIMEOUT_NO_VISITOR
        return None
