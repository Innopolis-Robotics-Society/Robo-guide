"""`dialog.interaction_log.build_interaction_record()` -- чистая логика (llm_plam.md §6)."""

from __future__ import annotations

from guide_robot_llm.dialog.interaction_log import build_interaction_record
from guide_robot_llm.dialog.loop import ReactTurnResult, ToolCallRecord


def _result(**overrides) -> ReactTurnResult:
    defaults = {
        "messages": [{"role": "system", "content": "s"}],
        "calls": [],
        "stopped_reason": "terminal_tool",
    }
    defaults.update(overrides)
    return ReactTurnResult(**defaults)


def test_record_carries_core_fields_verbatim() -> None:
    record = build_interaction_record(
        turn_id=42,
        mission_state_name="IDLE",
        utterance="привет",
        snapshot={"mission": {"state": "IDLE"}},
        result=_result(),
        stage_timings=[],
        degraded=False,
        degrade_reason=None,
        total_ms=850.2,
        now_s=1730000000.0,
    )

    assert record["turn_id"] == 42
    assert record["mission_state"] == "IDLE"
    assert record["utterance"] == "привет"
    assert record["snapshot"] == {"mission": {"state": "IDLE"}}
    assert record["stopped_reason"] == "terminal_tool"
    assert record["degraded"] is False
    assert record["degrade_reason"] is None
    assert record["total_ms"] == 850.2
    assert record["ts"] == 1730000000.0


def test_calls_are_serialized_with_content_version_always_none() -> None:
    call = ToolCallRecord(
        name="say", args={"text": "hi"}, result_ok=True, result_message="ok", result_data={}
    )
    record = build_interaction_record(
        turn_id=1,
        mission_state_name="IDLE",
        utterance="x",
        snapshot={},
        result=_result(calls=[call]),
        stage_timings=[],
        degraded=False,
        degrade_reason=None,
        total_ms=1.0,
        now_s=0.0,
    )

    assert record["calls"] == [
        {
            "tool": "say",
            "args": {"text": "hi"},
            "ok": True,
            "message": "ok",
            "content_version": None,
        }
    ]


def test_stage_timings_passed_through_unmodified_as_flat_list() -> None:
    timings = [
        {"stage": "llm_call", "tool": None, "ms": 812.3},
        {"stage": "tool_call", "tool": "say", "ms": 12.1},
    ]
    record = build_interaction_record(
        turn_id=1,
        mission_state_name="IDLE",
        utterance="x",
        snapshot={},
        result=_result(),
        stage_timings=timings,
        degraded=False,
        degrade_reason=None,
        total_ms=1.0,
        now_s=0.0,
    )

    assert record["stage_timings"] == timings


def test_degraded_flag_and_reason_propagate_verbatim() -> None:
    record = build_interaction_record(
        turn_id=1,
        mission_state_name="ANSWERING",
        utterance="x",
        snapshot={},
        result=_result(stopped_reason="aborted"),
        stage_timings=[],
        degraded=True,
        degrade_reason="aborted",
        total_ms=1.0,
        now_s=0.0,
    )

    assert record["degraded"] is True
    assert record["degrade_reason"] == "aborted"
    assert record["stopped_reason"] == "aborted"


def test_multiple_calls_preserve_order() -> None:
    calls = [
        ToolCallRecord(
            name="list_locations", args={}, result_ok=True, result_message="", result_data={}
        ),
        ToolCallRecord(
            name="say", args={"text": "ok"}, result_ok=True, result_message="", result_data={}
        ),
    ]
    record = build_interaction_record(
        turn_id=1,
        mission_state_name="IDLE",
        utterance="x",
        snapshot={},
        result=_result(calls=calls),
        stage_timings=[],
        degraded=False,
        degrade_reason=None,
        total_ms=1.0,
        now_s=0.0,
    )

    assert [c["tool"] for c in record["calls"]] == ["list_locations", "say"]
