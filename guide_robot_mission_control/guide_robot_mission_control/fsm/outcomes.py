"""Строковые исходы состояний верхней SM (design §5.2), общие для всех состояний.

Одно место объявления -- чтобы `root_sm.py` и конкретные состояния не могли
разойтись в написании строки-исхода (опечатка превращается в
`RuntimeError` из `root_sm.run_tour()`, а не в тихо забытый переход).

`CANCELED` и `HELD` производятся УНИВЕРСАЛЬНО базовым классом
(`fsm/base.py`), не самими состояниями -- design §5.4 правило 5 ("HELD
вытесняет всё") и требование "отмена RunTour в каждом состоянии"
(design §9.2) иначе пришлось бы дублировать в каждом poll().
"""

from __future__ import annotations

__all__ = [
    "ABORTED",
    "ANSWERED",
    "ARRIVED",
    "CANCELED",
    "CLEARED",
    "END_TOUR",
    "HELD",
    "HOLD_TIMEOUT",
    "INTERRUPTED",
    "NARRATE_FAILED",
    "NAV_FAILED",
    "NO",
    "PAUSED",
    "RESUMED",
    "SHUTDOWN",
    "SKIP_STOP",
    "SUCCEEDED",
    "TIMEOUT",
    "TIMEOUT_NO_VISITOR",
    "TOUR_FINISHED",
    "YES",
]

SUCCEEDED = "succeeded"
ABORTED = "aborted"
INTERRUPTED = "interrupted"
ANSWERED = "answered"
# SubmitAnswer.OUTCOME_SKIP_STOP/OUTCOME_END_TOUR (guide_robot_llm/llm_plam.md
# §1.1) -- ANSWERED и так означало "вернуться в прерванное состояние", эти
# два делят исход ANSWERING на явные альтернативы вместо одного значения.
SKIP_STOP = "skip_stop"
END_TOUR = "end_tour"
TIMEOUT = "timeout"
SHUTDOWN = "shutdown"

ARRIVED = "arrived"
NAV_FAILED = "nav_failed"
NARRATE_FAILED = "narrate_failed"
TOUR_FINISHED = "tour_finished"
YES = "yes"
NO = "no"
CANCELED = "canceled"
HELD = "held"
CLEARED = "cleared"
HOLD_TIMEOUT = "hold_timeout"
PAUSED = "paused"
RESUMED = "resumed"
TIMEOUT_NO_VISITOR = "timeout_no_visitor"
