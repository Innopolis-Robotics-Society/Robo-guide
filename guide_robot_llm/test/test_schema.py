"""Таблица гейтов tools/schema.py (llm_plam.md §4) -- каждый инструмент x состояние."""

from __future__ import annotations

from guide_robot_llm.tools.schema import allowed_tools, is_tool_allowed

from guide_robot_msgs.msg import MissionState

_S = MissionState


def test_start_tour_only_allowed_idle() -> None:
    assert is_tool_allowed("start_tour", _S.STATE_IDLE)
    assert not is_tool_allowed("start_tour", _S.STATE_NAVIGATING)
    assert not is_tool_allowed("start_tour", _S.STATE_NARRATING)


def test_stop_tour_allowed_everywhere_except_idle() -> None:
    assert not is_tool_allowed("stop_tour", _S.STATE_IDLE)
    for state in (
        _S.STATE_GREETING,
        _S.STATE_NAVIGATING,
        _S.STATE_NARRATING,
        _S.STATE_ANSWERING,
        _S.STATE_AWAITING_CONFIRM,
        _S.STATE_PAUSED,
        _S.STATE_HELD,
        _S.STATE_RETURNING,
    ):
        assert is_tool_allowed("stop_tour", state)


def test_pause_only_allowed_narrating() -> None:
    assert is_tool_allowed("pause", _S.STATE_NARRATING)
    assert not is_tool_allowed("pause", _S.STATE_NAVIGATING)
    assert not is_tool_allowed("pause", _S.STATE_PAUSED)


def test_resume_only_allowed_paused() -> None:
    assert is_tool_allowed("resume", _S.STATE_PAUSED)
    assert not is_tool_allowed("resume", _S.STATE_NARRATING)


def test_confirm_only_allowed_awaiting_confirm() -> None:
    assert is_tool_allowed("confirm", _S.STATE_AWAITING_CONFIRM)
    assert not is_tool_allowed("confirm", _S.STATE_NARRATING)


def test_finish_answer_only_allowed_answering() -> None:
    assert is_tool_allowed("finish_answer", _S.STATE_ANSWERING)
    assert not is_tool_allowed("finish_answer", _S.STATE_AWAITING_CONFIRM)


def test_say_allowed_in_every_state() -> None:
    for state in range(9):
        assert is_tool_allowed("say", state)


def test_tell_about_only_allowed_idle() -> None:
    assert is_tool_allowed("tell_about", _S.STATE_IDLE)
    assert not is_tool_allowed("tell_about", _S.STATE_NARRATING)


def test_read_only_tools_allowed_in_every_state() -> None:
    for name in ("list_locations", "list_tours", "estimate_route"):
        for state in range(9):
            assert is_tool_allowed(name, state)


def test_unknown_tool_never_allowed() -> None:
    assert not is_tool_allowed("does_not_exist", _S.STATE_IDLE)


def test_allowed_tools_idle_matches_expected_set() -> None:
    assert set(allowed_tools(_S.STATE_IDLE)) == {
        "start_tour",
        "guide_to",
        "tour_by_points",
        "tell_about",
        "say",
        "list_locations",
        "list_tours",
        "estimate_route",
    }
