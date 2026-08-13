"""`dialog.interaction_log.build_interaction_record()` -- схема v3 (DIALOG_REWORK_PLAN.md §8)."""

from __future__ import annotations

from guide_robot_llm.dialog.interaction_log import build_interaction_record
from guide_robot_llm.dialog.turn import ToolCallRecord, TurnResult


def _result(**overrides) -> TurnResult:
    defaults = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "Привет!"},
        ],
        "answer_text": "Привет!",
        "answer_raw_text": "Привет!",
        "answer_finish_reason": "stop",
        "action_raw_text": '{"tool": "noop", "args": {}}',
        "action_finish_reason": "stop",
        "say_ok": True,
        "action": None,
        "repair_used": False,
        "stopped_reason": "ok",
    }
    defaults.update(overrides)
    return TurnResult(**defaults)


def _base_kwargs(**overrides) -> dict:
    defaults = {
        "turn_id": 1,
        "session_id": "abc123def456",
        "utterance_ts": 1729999999.5,
        "mission_state_name": "IDLE",
        "utterance": "привет",
        "snapshot": {"mission": {"state": "IDLE"}},
        "references": [],
        "corpus_texts": [],
        "result": _result(),
        "stage_timings": [],
        "history_entries": 2,
        "history_cleared": False,
        "told_ids": [],
        "degraded": False,
        "degrade_reason": None,
        "total_ms": 850.2,
        "now_s": 1730000000.0,
    }
    defaults.update(overrides)
    return defaults


def test_record_carries_core_fields_verbatim() -> None:
    record = build_interaction_record(**_base_kwargs(turn_id=42, mission_state_name="IDLE"))

    assert record["schema_version"] == 4
    assert record["turn_id"] == 42
    assert record["session_id"] == "abc123def456"
    assert record["utterance_ts"] == 1729999999.5
    assert record["mission_state"] == "IDLE"
    assert record["utterance"] == "привет"
    assert record["snapshot"] == {"mission": {"state": "IDLE"}}
    assert record["answer_text"] == "Привет!"
    assert record["answer_chars"] == len("Привет!")
    assert record["say_ok"] is True
    assert record["stopped_reason"] == "ok"
    assert record["degraded"] is False
    assert record["degrade_reason"] is None
    assert record["total_ms"] == 850.2
    assert record["ts"] == 1730000000.0


def test_record_carries_raw_llm_input_and_output() -> None:
    """По запросу: в логе нужно видеть буквально всё, что получила и выдала модель --
    полный обмен сообщениями и сырой (несанитайзенный) текст обеих фаз."""
    result = _result(
        messages=[
            {"role": "system", "content": "преамбул"},
            {"role": "user", "content": "снимок"},
            {"role": "assistant", "content": "# Заголовок\nПривет!"},
            {"role": "user", "content": "PHASE2"},
            {"role": "assistant", "content": '{"tool": "noop", "args": {}}'},
        ],
        answer_text="Заголовок Привет!",
        answer_raw_text="# Заголовок\nПривет!",
        answer_finish_reason="stop",
        action_raw_text='{"tool": "noop", "args": {}}',
        action_finish_reason="stop",
    )
    record = build_interaction_record(**_base_kwargs(result=result))

    assert record["llm_messages"] == result.messages
    assert record["answer_raw_text"] == "# Заголовок\nПривет!"
    assert record["answer_finish_reason"] == "stop"
    assert record["action_raw_text"] == '{"tool": "noop", "args": {}}'
    assert record["action_finish_reason"] == "stop"


def test_action_raw_text_present_even_when_action_is_none() -> None:
    """action_parse_error: `action` -- None, но модель что-то прислала -- это
    единственное место, где виден её сырой (невалидный) ответ."""
    result = _result(
        action=None, action_raw_text="это не json", stopped_reason="action_parse_error"
    )
    record = build_interaction_record(**_base_kwargs(result=result))

    assert record["action"] is None
    assert record["action_raw_text"] == "это не json"


def test_action_none_when_turn_result_has_no_action() -> None:
    record = build_interaction_record(**_base_kwargs(result=_result(action=None)))
    assert record["action"] is None


def test_action_serialized_with_content_version_always_none() -> None:
    call = ToolCallRecord(
        name="noop", args={}, result_ok=True, result_message="", result_data={}
    )
    record = build_interaction_record(**_base_kwargs(result=_result(action=call)))

    assert record["action"] == {
        "tool": "noop",
        "args": {},
        "think": "",
        "ok": True,
        "message": "",
        "content_version": None,
    }


def test_action_think_passes_through_to_record() -> None:
    """v4: think -- готовая диагностика «почему выбрана эта ветка»."""
    call = ToolCallRecord(
        name="guide_to",
        args={"location_id": "cafe"},
        result_ok=True,
        result_message="",
        result_data={},
        think="посетитель просит отвести к кафе",
    )
    record = build_interaction_record(**_base_kwargs(result=_result(action=call)))

    assert record["action"]["think"] == "посетитель просит отвести к кафе"


def test_references_output_omits_text_keeps_id_heading_score() -> None:
    """Ретрив из хода убран (CLAUDE_CODE_TASK.md п.5) -- `references` в схеме остался,
    но обычно приходит пустым; поле всё ещё умеет отдать id/heading/score без text,
    если вызывающий когда-нибудь снова начнёт его наполнять."""
    references = [
        {"id": "kb_0007", "heading": "Иннополис > Кампус", "text": "полный текст", "score": 4.2}
    ]
    record = build_interaction_record(**_base_kwargs(references=references))

    assert record["references"] == [
        {"id": "kb_0007", "heading": "Иннополис > Кампус", "score": 4.2}
    ]
    assert "текст" not in str(record["references"])


def test_verbatim_overlap_computed_against_full_corpus_texts() -> None:
    """CLAUDE_CODE_TASK.md п.5: метрика считается против полного текста корпуса,
    а не против (теперь всегда пустого) per-turn `references`."""
    corpus_texts = ["город основан указом президента"]
    result = _result(answer_text="город основан указом президента")
    record = build_interaction_record(
        **_base_kwargs(references=[], corpus_texts=corpus_texts, result=result)
    )

    assert record["verbatim_overlap_words"] == 4


def test_verbatim_overlap_zero_without_corpus() -> None:
    record = build_interaction_record(**_base_kwargs(corpus_texts=[]))
    assert record["verbatim_overlap_words"] == 0


def test_history_and_told_ids_pass_through() -> None:
    record = build_interaction_record(
        **_base_kwargs(history_entries=6, history_cleared=True, told_ids=["lab_demo", "cafe"])
    )
    assert record["history_entries"] == 6
    assert record["history_cleared"] is True
    assert record["told_ids"] == ["lab_demo", "cafe"]


def test_repair_used_passes_through() -> None:
    record = build_interaction_record(**_base_kwargs(result=_result(repair_used=True)))
    assert record["repair_used"] is True


def test_stage_timings_passed_through_unmodified_as_flat_list() -> None:
    timings = [
        {"stage": "retrieve", "ms": 3.1},
        {"stage": "llm_answer", "ms": 812.3},
        {"stage": "say", "ms": 11.0},
    ]
    record = build_interaction_record(**_base_kwargs(stage_timings=timings))
    assert record["stage_timings"] == timings


def test_degraded_flag_and_reason_propagate_verbatim() -> None:
    record = build_interaction_record(
        **_base_kwargs(
            mission_state_name="ANSWERING",
            result=_result(stopped_reason="aborted"),
            degraded=True,
            degrade_reason="aborted",
        )
    )
    assert record["degraded"] is True
    assert record["degrade_reason"] == "aborted"
    assert record["stopped_reason"] == "aborted"
