"""HELD (design §5.2, §5.4 правило 5, §5.7): safety-стоп вытесняет всё.

Вход сюда обрабатывает база (`fsm/base.py`, `_poll_loop`) единообразно
для ЛЮБОГО состояния -- само `HeldState` только ждёт снятия
`safety_hold_event` или `held_max_s`. Explaining-фраза (design §5.7,
"Объяснение (L2) произносится отдельным Say scope=system") -- вне рамок
шага 7 (в `config/mission.yaml`/`phrases_ru.yaml` пока не заведено).

`held_resume_reannounce_s` (design §5.7: мостовая фраза, если простояли
дольше) -- тоже отложено, симметрично `resume_bridge_enabled` у
narration_server: сюда добавится тем же способом, отдельным Say.
"""

from __future__ import annotations

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["HeldState"]


class HeldState(InterruptibleState):
    """Ждёт снятия safety_hold_event либо held_max_s."""

    name = "held"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Запомнить момент входа -- отсчёт `held_max_s` идёт от него."""
        del blackboard
        self._start_ns = self.ctx.now_ns()

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Вернуть CLEARED, если estop снят; HOLD_TIMEOUT, если простояли дольше held_max_s."""
        del blackboard
        if not self.ctx.safety_hold_event.is_set():
            return outcomes.CLEARED
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s >= self.ctx.held_max_s:
            return outcomes.HOLD_TIMEOUT
        return None
