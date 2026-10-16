"""ANSWERING (design §5.2, §5.4): фрейм стека прерываний глубины 1.

`base_state` фрейма -- `blackboard.interrupted_from`, записанный
`root_sm.run_tour()` в момент перехода сюда (GREETING/NAVIGATING/NARRATING
все могут прерваться в ANSWERING -- design §5.2, поэтому целевое
состояние резюме не может быть захардкожено, как в шаге 6).

Правило 3 (design §5.4): повторный barge-in, пока висим здесь, не пушит
второй фрейм -- `InterruptStack.on_barge_in()` просто сдвигает `opened_at`,
и `answer_max_s` отсчитывается заново от него (`answer_timed_out()` сам
это учитывает, читая текущий `opened_at` фрейма).

`ANSWERED` в v1 не имеет реального источника (нет LLM/AskUser-связки,
design §5.4 п.6 оговаривает это явно) -- путь существует ради контракта и
тестируется через `FsmContext.submit_answer()`.
"""

from __future__ import annotations

from guide_robot_msgs.srv import SubmitAnswer

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["AnsweringState"]

# SubmitAnswer.Request.OUTCOME_* -> исход верхней SM (root_sm._TRANSITIONS).
# OUTCOME_RESUME_BASE по умолчанию у FsmContext.submit_answer -- тот же путь,
# что и раньше, когда outcome не существовал.
_OUTCOME_TO_SM = {
    SubmitAnswer.Request.OUTCOME_RESUME_BASE: outcomes.ANSWERED,
    SubmitAnswer.Request.OUTCOME_SKIP_STOP: outcomes.SKIP_STOP,
    SubmitAnswer.Request.OUTCOME_END_TOUR: outcomes.END_TOUR,
}


class AnsweringState(InterruptibleState):
    """Держит answer-фрейм, пока не придёт ответ или не истечёт answer_max_s."""

    name = "answering"
    redirect_eligible = True

    def on_enter(self, blackboard: Blackboard) -> None:
        """Открыть answer-фрейм с base_state = состояние, которое было прервано."""
        now_s = self.ctx.now_ns() / 1e9
        blackboard.stack.push_answer(
            base_state=blackboard.interrupted_from, resume_token=blackboard.resume_token, now=now_s
        )

    def on_exit(self, blackboard: Blackboard, outcome: str) -> None:
        """Снять езду, оставшуюся с NAVIGATING, если тур дальше к той же остановке не идёт."""
        carry = blackboard.nav_carry
        if carry is None or outcome in (
            outcomes.ANSWERED,
            outcomes.TIMEOUT,
            outcomes.SKIP_STOP,
        ):
            return
        if not carry.cancelled:
            carry.cancel()
        blackboard.nav_carry = None

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """REDIRECTED (stage2 B2): тур продолжается по новому плану -- снять answer-фрейм.

        Не тронуто для CANCELED/HELD -- тур либо заканчивается совсем
        (CANCELED теперь терминален, fsm/root_sm.py::_UNIVERSAL), либо
        фрейм обязан пережить HELD как есть (design правило 5).
        """
        if outcome == outcomes.REDIRECTED:
            blackboard.stack.pop()

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Ждать ответ/повторный barge-in/answer_max_s -- см. докстринг класса."""
        now_s = now_ns / 1e9

        answer = self.ctx.take_answer()
        if answer is not None:
            text, sm_outcome = answer
            self.ctx.log(f"answering: получен ответ -- {text!r} (outcome={sm_outcome})")
            blackboard.last_answer = text
            blackboard.stack.pop()
            return _OUTCOME_TO_SM.get(sm_outcome, outcomes.ANSWERED)

        # hold_position в ANSWERING имеет смысл только на ходу; без живой езды
        # запрос забирается и выбрасывается, чтобы не остановить следующий NAVIGATING.
        stop_driving = self.ctx.take_pause_request()
        barge_in = self.ctx.take_barge_in()
        if barge_in is not None:
            blackboard.stack.on_barge_in(now=now_s)
            stop_driving = stop_driving or barge_in == "stop"
        carry = blackboard.nav_carry
        if stop_driving and carry is not None and not carry.cancelled:
            # Стоп-слово/«подожди» во время ответа на ходу: встать, как
            # hold_position из NAVIGATING. PAUSED -> resume вернёт NAVIGATING на
            # ту же остановку (root_sm сохраняет interrupted_from="navigating").
            carry.cancel()
            blackboard.stack.pop()
            blackboard.pause_reason = "user"
            return outcomes.PAUSED
        if barge_in is not None:
            return None

        if blackboard.stack.answer_timed_out(now=now_s, answer_max_s=self.ctx.answer_max_s):
            self.ctx.log(f"answering: answer_max_s={self.ctx.answer_max_s} истёк, ответа не было")
            blackboard.stack.pop()
            return outcomes.TIMEOUT

        return None
