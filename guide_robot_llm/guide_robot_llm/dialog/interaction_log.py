"""Сборка одной jsonl-записи интеракции из `TurnResult` -- схема v6 (DIALOG_REWORK_PLAN.md §8).

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

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from guide_robot_llm.dialog.verbatim import max_shingle_overlap
from guide_robot_llm.llm_client.redact import redact_messages

if TYPE_CHECKING:
    from guide_robot_llm.dialog.turn import TurnResult

__all__ = ["SCHEMA_VERSION", "SchemaVersionError", "build_interaction_record", "load_record"]

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
# v5: `references` сменил форму с {id, heading, score} (пассаж старого
# kb.jsonl) на {content_id, chunk_id, score, source} -- источник фактов
# теперь guide_robot_semantic_map/content/, единица -- чанк, не пассаж;
# `source` различает автосправку ("auto", CLAUDE_CODE_TASK_stage1_knowledge.md
# п.7.1) от явного read_only-вызова модели ("tool", п.7.2). Обратная
# совместимость со старой формой не нужна -- единственный потребитель
# (`kb.jsonl`) удалён вместе с корпусом.
# v6 (Taiga #4): `llm_messages` теперь идёт через `redact_messages`
# (llm_client.redact) -- base64-payload'ы data-URL'ов кадров маскируются, в
# текстовый лог не попадает (acceptance issue #4); добавлен опциональный
# блок `observation` (фаза observe_then_decide: сырой вывод, рендер,
# причина деградации).
SCHEMA_VERSION = 6


class SchemaVersionError(ValueError):
    """Запись лога несовместимой версии схемы."""


def load_record(line: str | dict) -> dict:
    """Разобрать запись лога и проверить версию схемы.

    `line` -- jsonl-строка или уже распарсенный dict. При
    `schema_version != SCHEMA_VERSION` бросает `SchemaVersionError` с
    внятным сообщением (какая версия в записи, какая поддерживается) вместо
    тихого чтения несовместимых полей (acceptance issue #8: старый ридер
    падает понятно, а не молча).
    """
    record = json.loads(line) if isinstance(line, str) else line
    if not isinstance(record, dict):
        raise SchemaVersionError("запись лога не является JSON-объектом")
    version = record.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"несовместимая версия схемы лога: запись v{version}, "
            f"поддерживается v{SCHEMA_VERSION}"
        )
    return record


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
    endpoint: str = "",
    model_name: str = "",
    prompt_strategy: str = "",
    episode_id: str | None = None,
) -> dict:
    """Собрать одну jsonl-запись хода диалога (схема v6).

    `references` -- все чанки, что модель видела в ходу: автосправка перед
    фазой действия (`source: "auto"`) + явный read_only-вызов, если модель
    его выбрала (`source: "tool"`) -- `dialog_agent_node.py::_run_turn`
    собирает оба списка и передаёт уже готовым (CLAUDE_CODE_TASK_stage1_
    knowledge.md п.7.1-7.3). Пустой список -- ничего не нашли ни разу за ход.

    `corpus_texts` -- тексты этих же чанков (parallel к `references`, без
    метаданных), против них считается `verbatim_overlap_words`: метрика
    «пересказ или дословная цитата» теперь per-turn, не против всего
    корпуса целиком (локального корпуса знаний больше нет, п.5).

    `action.content_version` -- версия контента из `result_data["version"]`,
    если read_only-вызов её вернул (`lookup_content`/`search_content` синхронны
    и несут версию в ответе); `None` для остальных инструментов --
    `tool_broker._tool_tell_about`/`_tool_say` не ждут результата
    `Narrate`/`Say` (fire-and-forget по дизайну), версия реально озвученного
    контента до `dialog_agent` не доходит.

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
            "content_version": result.action.result_data.get("version"),
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
            {
                "content_id": ref["content_id"],
                "chunk_id": ref["chunk_id"],
                "score": ref["score"],
                "source": ref["source"],
            }
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
        # Таига #4: фаза наблюдения (observe_then_decide) -- только если
        # прогонялась или деградировала (direct_action/text-only не несут).
        "observation": (
            {
                "raw": result.observation_raw_text,
                "text": result.observation_text,
                "error": result.observation_error,
            }
            if (result.observation_raw_text or result.observation_text or result.observation_error)
            else None
        ),
        "repair_used": result.repair_used,
        # Таига #4: base64 кадров в текстовый лог не попадает -- content-
        # массивы с image_url маскируются (redact_messages, Taiga #3).
        "llm_messages": redact_messages(result.messages),
        "history_entries": history_entries,
        "history_cleared": history_cleared,
        "told_ids": list(told_ids),
        "stage_timings": stage_timings,
        "stopped_reason": result.stopped_reason,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "total_ms": total_ms,
        "endpoint": {"base_url": endpoint, "model": model_name},
        "prompt_strategy": prompt_strategy,
        "frame_count": len(snapshot.get("frames", [])),
        "schema_valid_raw": result.action_first_attempt_valid,
        "validator_reason": result.action_reason_code,
        "episode_id": episode_id,
    }
