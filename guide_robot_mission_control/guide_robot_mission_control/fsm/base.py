"""`InterruptibleState` -- базовый класс длинных состояний верхней SM (design §5.1).

Ни один `poll()` не имеет права блокироваться: внутри -- поллинг с шагом
`poll_period_s` (design: 20 мс), не `spin_until_future_complete`.

`CANCELED` (клиент отменил `RunTour`-goal) и `HELD` (design §5.4 правило
5, "HELD вытесняет всё") проверяются здесь ЖЁСТКО единообразно для всех
состояний -- ни один конкретный `poll()` не обязан помнить об этом сам.
Состояние переопределяет `cancel_active_work()`, только если у него есть
что отменять (активный `Say`/`Narrate`/`NavigateToPose` goal).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from guide_robot_mission_control.fsm import outcomes

if TYPE_CHECKING:
    from guide_robot_mission_control.fsm.blackboard_keys import Blackboard
    from guide_robot_mission_control.fsm.context import FsmContext

__all__ = ["InterruptibleState"]


class InterruptibleState:
    """Один узел верхней SM. Подклассы переопределяют `on_enter`/`poll`/`on_exit`."""

    name: str = "state"
    # stage2 B2: только эти состояния принимают ~/redirect (design блок B) --
    # проверяется здесь же, где CANCELED/HELD, теми же средствами.
    redirect_eligible: bool = False

    def __init__(self, ctx: FsmContext) -> None:
        """Запомнить контекст (очереди/флаги/клиенты) -- своё состояние заводит подкласс."""
        self.ctx = ctx

    def on_enter(self, blackboard: Blackboard) -> None:
        """Инициализировать состояние (отправить goal и т.п.). По умолчанию -- ничего."""

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Вернуть исход, если он уже готов, иначе None -- цикл продолжится."""
        raise NotImplementedError

    def on_exit(self, blackboard: Blackboard, outcome: str) -> None:
        """Освободить ресурсы состояния. По умолчанию -- ничего."""

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """Отменить активный goal при CANCELED/HELD (см. `outcome`). По умолчанию -- ничего.

        `outcome` различим намеренно: HELD обязан сохранить фрейм стека
        как есть (design §5.4 правило 5), CANCELED -- нет, тур
        заканчивается совсем. Состояния без стека (NAVIGATING/NARRATING/
        GREETING) обычно реагируют одинаково на оба случая и просто
        отменяют свой goal.
        """

    def run(self, blackboard: Blackboard) -> str:
        """Прогнать состояние целиком: on_enter -> поллинг -> on_exit. Возвращает исход."""
        self.on_enter(blackboard)
        self.ctx.on_state_changed(self.name, blackboard)
        outcome = self._poll_loop(blackboard)
        self.on_exit(blackboard, outcome)
        return outcome

    def _poll_loop(self, blackboard: Blackboard) -> str:
        while True:
            if self.ctx.deactivating_event.is_set():
                return outcomes.SHUTDOWN
            if self.ctx.is_cancel_requested():
                self.cancel_active_work(blackboard, outcomes.CANCELED)
                return outcomes.CANCELED
            if self.name != "held" and self.ctx.safety_hold_event.is_set():
                self.cancel_active_work(blackboard, outcomes.HELD)
                return outcomes.HELD
            if self.redirect_eligible:
                location_id = self.ctx.take_redirect_request()
                if location_id is not None:
                    blackboard.redirect_location_id = location_id
                    self.cancel_active_work(blackboard, outcomes.REDIRECTED)
                    return outcomes.REDIRECTED
            outcome = self.poll(blackboard, self.ctx.now_ns())
            if outcome is not None:
                return outcome
            time.sleep(self.ctx.poll_period_s)
