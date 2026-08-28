"""Сборка одной jsonl-записи интеракции из `TurnResult` -- схема v3 (DIALOG_REWORK_PLAN.md §8).

Чистая логика без rclpy -- тестируется на голом `TurnResult` без ROS/HTTP,
`dialog_agent_node.py` -- единственный потребитель из rclpy-контекста (он же
собирает `stage_timings`/`references`/`told_ids`/`history_entries` и решает
`degraded`/`degrade_reason`).

`stage_timings` -- отдельный плоский хронологический список, не вложенный
per-call breakdown: ход может остановиться на `action_parse_error`/
`action_backend_error` ПОСЛЕ уже состоявшегося вызова бэкенда, но ДО того,
как `execute_tool()` вообще позвался -- в этом случае в `result.action` не
будет записи, которой можно было бы приписать тайминг того вызова. Плоский
список не теряет эту информацию и не требует хрупкого сопоставления по
индексу.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from guide_robot_llm.dialog.verbatim import max_shingle_overlap

if TYPE_CHECKING:
    from guide_robot_llm.dialog.turn import TurnResult

__all__ = ["SCHEMA_VERSION", "build_interaction_record"]

# v3: добавлены `llm_messages` (полный обмен с ЛЛМ за ход) и
# `answer_raw_text`/`answer_finish_reason`/`action_raw_text`/
# `action_finish_reason` (сырой, ещё не санитайзенный вывод модели) --
# по запросу: в логе нужно видеть буквально всё, что получила и выдала
# модель, не только то, что дошло до озвучки/действия.
# v4: `action.think` -- явное рассуждение модели перед выбором инструмента
# (ReAct-Thought из GBNF-формы фазы действия): готовая диагностика «почему
# выбрана эта ветка», прежде провалы выбора разбирались по косвенным уликам.
# Также `session_id` (сессия конфигурации dialog_agent) и `utterance_ts`
# (момент приёма транскрипта): `(session_id, turn_id)` глобально уникальна --
# перезапуск dialog_agent при живом interaction_log больше не производит
# записей, неотличимых от дублей.
SCHEMA_VERSION = 4


def build_interaction_record(
    *,
    turn_id: int,
    session_id: str,
    utterance_ts: float,
    mission_state_name: str,
    utterance: str,
    snapshot: dict,
    references: Sequence[dict],
    corpus_texts: Sequence[str],
    result: TurnResult,
    stage_timings: list[dict],
    history_entries: int,
    history_cleared: bool,
    told_ids: Sequence[str],
    degraded: bool,
    degrade_reason: str | None,
    total_ms: float,
    now_s: float,
) -> dict:
    """Собрать одну jsonl-запись хода диалога (схема v2).

    `references` -- CLAUDE_CODE_TASK.md п.5: ретрив из хода убран (корпус
    целиком идёт в системный промпт, секция «Справочник»), поэтому вызывающий
    всегда передаёт `[]` -- поле оставлено в схеме лога ради обратной
    совместимости, а не потому что что-то в него пишет.

    `corpus_texts` -- полный текст корпуса (по пассажу: `heading` + `text`),
    против него считается `verbatim_overlap_words`: метрика «пересказ или
    дословная цитата» по-прежнему осмысленна и без per-turn ретрива --
    достаточно знать, что модель имела в виду весь справочник целиком.

    `action.content_version` всегда `None` -- известный, задокументированный
    пробел: `tool_broker._tool_tell_about`/`_tool_say` не ждут результата
    `Narrate`/`Say` (fire-and-forget по дизайну), поэтому версия реально
    озвученного контента (`GetExhibitContent`) никогда не доходит обратно
    до `dialog_agent`.

    `llm_messages` -- `result.messages` как есть: весь обмен с ЛЛМ за ход
    (system prompt, история, реплика посетителя, сырой tool-call на КАЖДОЙ
    попытке починки, инструкция и сырой текст фазы реплики). `answer_raw_text`/
    `action_raw_text` дублируют последнюю реплику/tool-call ИЗ этого же
    списка отдельными полями -- удобства ради (не парсить `llm_messages`,
    чтобы узнать, что модель ответила ДО `sanitize_answer`/до провала
    парсинга JSON). `action_raw_text`/`action_finish_reason` заполнены и
    когда `action is None` (`action_parse_error`) -- это единственное место,
    где виден сырой ответ модели в этом случае.
    """
    verbatim_overlap_words = max_shingle_overlap(result.answer_text, corpus_texts)

    action = None
    if result.action is not None:
        action = {
            "tool": result.action.name,
            "args": result.action.args,
            "think": result.action.think,
            "ok": result.action.result_ok,
            "message": result.action.result_message,
            "content_version": None,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now_s,
        "turn_id": turn_id,
        "session_id": session_id,
        "utterance_ts": utterance_ts,
        "mission_state": mission_state_name,
        "utterance": utterance,
        "snapshot": snapshot,
        "references": [
            {"id": ref["id"], "heading": ref["heading"], "score": ref["score"]}
            for ref in references
        ],
        "answer_text": result.answer_text,
        "answer_chars": len(result.answer_text),
        "answer_raw_text": result.answer_raw_text,
        "answer_finish_reason": result.answer_finish_reason,
        "action_raw_text": result.action_raw_text,
        "action_finish_reason": result.action_finish_reason,
        "verbatim_overlap_words": verbatim_overlap_words,
        "say_ok": result.say_ok,
        "action": action,
        "repair_used": result.repair_used,
        "llm_messages": result.messages,
        "history_entries": history_entries,
        "history_cleared": history_cleared,
        "told_ids": list(told_ids),
        "stage_timings": stage_timings,
        "stopped_reason": result.stopped_reason,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "total_ms": total_ms,
    }
