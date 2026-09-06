"""Верхняя SM -- маршрутизация исходов состояний в следующее состояние (design §5.2).

Один прогон = один `RunTour`-goal: `run_tour()` стартует в GREETING (или
сразу в NAVIGATING, если `tour.greet=False`) и завершается, когда очередное
состояние отдаёт исход, не ведущий никуда (`_TRANSITIONS[state][outcome]
is None`) -- design §5.2 не описывает явного "конца тура", им становится
RETURNING со своими SUCCEEDED/ABORTED/CANCELED.

`RESUME_BASE` -- design §5.2's "resume_base": не состояние, а
псевдо-переход, снимающий верхний фрейм стека и возвращающий управление в
`frame.base_state`. Если фрейма уже нет (сняли до этого, например
ANSWERING сама выполнила `stack.pop()` перед ANSWERED/TIMEOUT), целью
служит `blackboard.interrupted_from` -- имя состояния, которое сам
`root_sm` запомнил в момент перехода в ANSWERING/HELD/PAUSED. Оба
источника обязаны совпадать, когда фрейм ещё жив (design §5.4 правило 5:
"фрейм сохраняется как есть" -- ХELD посреди ANSWERING/AWAITING_CONFIRM не
трогает чужой фрейм, поэтому по возврату из HELD резюме идёт по фрейму, а
не по "held" как таковому).

Неизвестный исход -- `RuntimeError`, не тихий выход: состояние обязано
возвращать только то, что перечислено для него в design §5.2/§9.2, иначе
это баг состояния (тот же принцип громкости, что у narration_server на
пути busy/rejected).
"""

from __future__ import annotations

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard
from guide_robot_mission_control.fsm.context import FsmContext
from guide_robot_mission_control.fsm.states.answering import AnsweringState
from guide_robot_mission_control.fsm.states.awaiting_confirm import AwaitingConfirmState
from guide_robot_mission_control.fsm.states.greeting import GreetingState
from guide_robot_mission_control.fsm.states.held import HeldState
from guide_robot_mission_control.fsm.states.narrating import NarratingState
from guide_robot_mission_control.fsm.states.navigating import NavigatingState
from guide_robot_mission_control.fsm.states.paused import PausedState
from guide_robot_mission_control.fsm.states.returning import ReturningState

__all__ = ["RootStateMachine"]

RESUME_BASE = "__resume_base__"
CONFIRM_OR_CONTINUE = "__confirm_or_continue__"
SKIP_STOP_PSEUDO = "__skip_stop__"

# Каждое прерываемое состояние обязано принимать CANCELED/HELD -- их
# производит база (fsm/base.py) для ЛЮБОГО состояния единообразно.
_UNIVERSAL = {outcomes.CANCELED: "returning", outcomes.HELD: "held"}

_TRANSITIONS: dict[str, dict[str, str | None]] = {
    "greeting": {
        outcomes.SUCCEEDED: "navigating",
        outcomes.INTERRUPTED: "answering",
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "navigating": {
        # NavigatingState сам решает NAV_FAILED vs TOUR_FINISHED (§5.5:
        # "пропустить" -- продвигает tour.index сама, сюда попадает уже
        # обновлённый индекс следующей остановки). Без транзитного
        # нарратива (§5.6, отложен) во время движения ничего не звучит --
        # barge-in здесь не отслеживается (нечего прерывать), поэтому
        # исхода INTERRUPTED у NAVIGATING в шаге 7 нет.
        outcomes.ARRIVED: "narrating",
        outcomes.NAV_FAILED: "navigating",
        outcomes.TOUR_FINISHED: "returning",
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "narrating": {
        # NarratingState сам решает succeeded/tour_finished и продвигает
        # tour.index; SUCCEEDED дальше ветвится по confirm_between_stops.
        # NARRATE_FAILED -- зеркало NAV_FAILED у NAVIGATING: контент не
        # нашёлся/Narrate отклонён, остановка пропущена, tour.index уже
        # продвинут самим NarratingState -- продолжаем с NAVIGATING на
        # следующую остановку, а не рушим тур.
        outcomes.SUCCEEDED: CONFIRM_OR_CONTINUE,
        outcomes.TOUR_FINISHED: "returning",
        outcomes.INTERRUPTED: "answering",
        outcomes.PAUSED: "paused",
        outcomes.NARRATE_FAILED: "navigating",
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "answering": {
        outcomes.ANSWERED: RESUME_BASE,
        outcomes.SKIP_STOP: SKIP_STOP_PSEUDO,
        outcomes.END_TOUR: "returning",
        outcomes.TIMEOUT: RESUME_BASE,
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "awaiting_confirm": {
        outcomes.YES: "navigating",
        outcomes.NO: "returning",
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "paused": {
        outcomes.RESUMED: RESUME_BASE,
        outcomes.TIMEOUT_NO_VISITOR: "returning",
        outcomes.SHUTDOWN: None,
        **_UNIVERSAL,
    },
    "held": {
        outcomes.CLEARED: RESUME_BASE,
        outcomes.HOLD_TIMEOUT: "returning",
        outcomes.CANCELED: "returning",
        outcomes.SHUTDOWN: None,
    },
    "returning": {
        outcomes.SUCCEEDED: None,
        outcomes.ABORTED: None,
        outcomes.CANCELED: None,
        outcomes.HELD: "held",
        outcomes.SHUTDOWN: None,
    },
}


def _resume_target(blackboard: Blackboard) -> str:
    frame = blackboard.stack.frame
    if frame is not None:
        return frame.base_state
    return blackboard.interrupted_from


def _skip_stop_target(blackboard: Blackboard) -> str:
    """SubmitAnswer.OUTCOME_SKIP_STOP: закончить текущую остановку, ехать дальше.

    Осмысленно, только когда прервали NARRATING -- "хватит рассказывать
    про этот экспонат". Прерывание GREETING не имеет текущего экспоната,
    которое можно пропустить -- вырождается в обычный resume (design этот
    выбор явно отдавал ЛЛМ, здесь он сведён к единственному случаю,
    который реально что-то значит).

    Отменять активный `Narrate` не нужно: к этому моменту barge-in уже
    остановил его сам (narration_server слушает `/speech/cancel_all`
    напрямую, см. докстринг `fsm/states/narrating.py`), а
    `blackboard.narrate_goal_handle` уже `None` -- `InterruptibleState.on_exit`
    отрабатывает раньше, чем `root_sm` решает следующий шаг.
    """
    if blackboard.interrupted_from != "narrating":
        return blackboard.interrupted_from
    blackboard.stops_skipped += 1
    if not blackboard.tour.has_next_stop:
        return "returning"
    blackboard.tour.index += 1
    blackboard.resume_token = ""
    return "navigating"


class RootStateMachine:
    """Прогоняет состояния друг за другом по `_TRANSITIONS`, начиная с GREETING/NAVIGATING."""

    def __init__(self, ctx: FsmContext) -> None:
        """Построить состояния поверх общего FsmContext (один на весь прогон тура)."""
        self.ctx = ctx
        self._states = {
            "greeting": GreetingState(ctx),
            "navigating": NavigatingState(ctx),
            "narrating": NarratingState(ctx),
            "answering": AnsweringState(ctx),
            "awaiting_confirm": AwaitingConfirmState(ctx),
            "paused": PausedState(ctx),
            "held": HeldState(ctx),
            "returning": ReturningState(ctx),
        }

    def run_tour(self, blackboard: Blackboard) -> str:
        """Прогнать весь тур, вернуть исход последнего исполненного состояния."""
        self.ctx.consume_barge_in()  # сбросить возможный хвост от предыдущего goal-а
        current: str | None = "greeting" if blackboard.tour.greet else "navigating"
        last_outcome = outcomes.SHUTDOWN
        while current is not None:
            last_outcome = self._states[current].run(blackboard)
            table = _TRANSITIONS[current]
            if last_outcome not in table:
                msg = f"состояние {current!r} вернуло необрабатываемый исход {last_outcome!r}"
                raise RuntimeError(msg)
            next_state = table[last_outcome]
            if last_outcome in (outcomes.INTERRUPTED, outcomes.HELD, outcomes.PAUSED):
                blackboard.interrupted_from = current
            if next_state == RESUME_BASE:
                next_state = _resume_target(blackboard)
            elif next_state == SKIP_STOP_PSEUDO:
                next_state = _skip_stop_target(blackboard)
            elif next_state == CONFIRM_OR_CONTINUE:
                next_state = (
                    "awaiting_confirm" if blackboard.tour.confirm_between_stops else "navigating"
                )
            current = next_state
        return last_outcome
