"""Грамматика resume_token и решение о точке возобновления (design §3.1-3.2)."""

from __future__ import annotations

import pytest

from guide_robot_mission_control.resume import (
    ResumeOutcome,
    ResumePolicy,
    ResumeToken,
    TokenError,
    apply_resume_policy,
    resolve_resume,
)


def test_round_trip_parse_format() -> None:
    token = ResumeToken(exhibit_id="lab105a", version="ab12cd34", chunk_idx=3, char_off=42)
    raw = token.format()
    assert raw == "v1|lab105a|ab12cd34|3|42"
    assert ResumeToken.parse(raw) == token


def test_empty_string_is_valid_start_over() -> None:
    assert ResumeToken.parse("") is None


@pytest.mark.parametrize(
    "raw",
    [
        "v1|lab105a|ab12cd34|3",              # мало полей
        "v1|lab105a|ab12cd34|3|1|extra",       # слишком много полей
        "v2|lab105a|ab12cd34|3|1",             # не та версия грамматики
        "v1||ab12cd34|3|1",                     # пустой exhibit_id
        "v1|lab105a||3|1",                       # пустой version
        "v1|lab105a|ab12cd34|x|1",                 # chunk_idx не число
        "v1|lab105a|ab12cd34|3|x",                   # char_off не число
        "v1|lab105a|ab12cd34|-1|1",                    # отрицательный chunk_idx
        "v1|lab105a|ab12cd34|3|-1",                      # отрицательный char_off
        "чепуха",
    ],
)
def test_malformed_token_raises(raw: str) -> None:
    with pytest.raises(TokenError):
        ResumeToken.parse(raw)


def test_resolve_resume_start_on_empty_token() -> None:
    decision = resolve_resume(None, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert decision.outcome is ResumeOutcome.START
    assert decision.start_chunk_idx == 0


def test_resolve_resume_rejects_foreign_exhibit_id() -> None:
    token = ResumeToken("other_exhibit", "v1", 1, 0)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert decision.outcome is ResumeOutcome.REJECTED


def test_resolve_resume_restarts_on_version_mismatch() -> None:
    token = ResumeToken("lab105a", "old-version", 3, 10)
    decision = resolve_resume(token, exhibit_id="lab105a", version="new-version", chunk_count=5)
    assert decision.outcome is ResumeOutcome.START
    assert decision.start_chunk_idx == 0
    assert decision.detail == "content_rev_changed"


def test_resolve_resume_out_of_bounds_is_already_complete() -> None:
    token = ResumeToken("lab105a", "v1", 5, 0)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert decision.outcome is ResumeOutcome.ALREADY_COMPLETE


def test_resolve_resume_normal_case() -> None:
    token = ResumeToken("lab105a", "v1", 2, 17)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert decision.outcome is ResumeOutcome.RESUME
    assert decision.start_chunk_idx == 2
    assert decision.char_off == 17


def test_policy_repeat_chunk_keeps_start_but_char_off_is_reporting_only() -> None:
    token = ResumeToken("lab105a", "v1", 2, 17)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    result = apply_resume_policy(decision, ResumePolicy.REPEAT_CHUNK, chunk_count=5)
    assert result.start_chunk_idx == 2
    assert result.char_off == 17  # для отчётности, не для точки старта


def test_policy_continue_next_skips_interrupted_chunk() -> None:
    token = ResumeToken("lab105a", "v1", 2, 17)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    result = apply_resume_policy(decision, ResumePolicy.CONTINUE_NEXT, chunk_count=5)
    assert result.start_chunk_idx == 3
    assert result.char_off == 0
    assert result.skipped_chunk_idx == 2


def test_policy_continue_next_on_last_chunk_is_already_complete() -> None:
    token = ResumeToken("lab105a", "v1", 4, 3)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    result = apply_resume_policy(decision, ResumePolicy.CONTINUE_NEXT, chunk_count=5)
    assert result.outcome is ResumeOutcome.ALREADY_COMPLETE
    assert result.skipped_chunk_idx == 4


def test_policy_overlap_1_backs_up_one_chunk() -> None:
    token = ResumeToken("lab105a", "v1", 2, 17)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    result = apply_resume_policy(decision, ResumePolicy.OVERLAP_1, chunk_count=5)
    assert result.start_chunk_idx == 1


def test_policy_overlap_1_floors_at_zero() -> None:
    token = ResumeToken("lab105a", "v1", 0, 5)
    decision = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    result = apply_resume_policy(decision, ResumePolicy.OVERLAP_1, chunk_count=5)
    assert result.start_chunk_idx == 0


def test_policy_does_not_touch_start_or_already_complete() -> None:
    start = resolve_resume(None, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert apply_resume_policy(start, ResumePolicy.CONTINUE_NEXT, chunk_count=5) == start

    token = ResumeToken("lab105a", "v1", 5, 0)
    complete = resolve_resume(token, exhibit_id="lab105a", version="v1", chunk_count=5)
    assert apply_resume_policy(complete, ResumePolicy.OVERLAP_1, chunk_count=5) == complete
