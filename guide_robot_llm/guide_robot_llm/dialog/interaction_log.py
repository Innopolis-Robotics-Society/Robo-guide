"""Сборка одной jsonl-записи интеракции из результата ReAct-хода (llm_plam.md §6).

Чистая логика без rclpy -- тот же принцип, что `dialog/prompt.py`: тестируется
на голом `ReactTurnResult` без ROS/HTTP, `dialog_agent_node.py` -- единственный
потребитель из rclpy-контекста (он же собирает `stage_timings` и решает
`degraded`/`degrade_reason` -- см. докстрины там).

`stage_timings` -- отдельный плоский хронологический список, не вложенный
per-call breakdown: `run_react_turn()` может остановиться на `parse_error`/
`backend_error` ПОСЛЕ уже состоявшегося вызова бэкенда, но ДО того, как
`execute_tool()` вообще позвался -- в этом случае в `result.calls` не будет
записи, которой можно было бы приписать тайминг того вызова. Плоский список
не теряет эту информацию и не требует хрупкого сопоставления по индексу.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from guide_robot_llm.dialog.loop import ReactTurnResult

__all__ = ["build_interaction_record"]


def build_interaction_record(
    *,
    turn_id: int,
    mission_state_name: str,
    utterance: str,
    snapshot: dict,
    result: ReactTurnResult,
    stage_timings: list[dict],
    degraded: bool,
    degrade_reason: str | None,
    total_ms: float,
    now_s: float,
) -> dict:
    """Собрать одну jsonl-запись хода диалога.

    `content_version` каждого вызова всегда `None` -- известный, задокументированный
    пробел: `tool_broker._tool_tell_about`/`_tool_say` не ждут результата
    `Narrate`/`Say` (llm_plam.md §3: "не блокируется на действиях, которые
    реально идут долго"), поэтому версия реально озвученного контента
    (`GetExhibitContent`) никогда не доходит обратно до `dialog_agent`.
    Прокидывать её означало бы менять fire-and-forget дизайн шага 3 --
    отдельная, самостоятельно заказываемая доработка, не эта.
    """
    calls = [
        {
            "tool": call.name,
            "args": call.args,
            "ok": call.result_ok,
            "message": call.result_message,
            "content_version": None,
        }
        for call in result.calls
    ]

    return {
        "ts": now_s,
        "turn_id": turn_id,
        "mission_state": mission_state_name,
        "utterance": utterance,
        "snapshot": snapshot,
        "calls": calls,
        "stage_timings": stage_timings,
        "stopped_reason": result.stopped_reason,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "total_ms": total_ms,
    }
