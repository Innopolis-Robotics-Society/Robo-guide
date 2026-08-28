"""Таблица гейтов tools/schema.py (DIALOG_REWORK_PLAN.md §4.3) -- каждый инструмент x состояние."""

from __future__ import annotations

from guide_robot_llm.tools.schema import allowed_tools, is_tool_allowed, tool_spec

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


def test_hold_position_only_allowed_navigating() -> None:
    """stage2 D3: только NAVIGATING реально вычитывает take_pause_request()."""
    assert is_tool_allowed("hold_position", _S.STATE_NAVIGATING)
    assert not is_tool_allowed("hold_position", _S.STATE_NARRATING)
    assert not is_tool_allowed("hold_position", _S.STATE_IDLE)


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


def test_ask_visitor_allowed_in_every_state() -> None:
    for state in range(9):
        assert is_tool_allowed("ask_visitor", state)


def test_guide_to_allowed_in_every_state() -> None:
    """stage2 B3: guide_to больше не IDLE-only -- различие решает брокер."""
    for state in range(9):
        assert is_tool_allowed("guide_to", state)


def test_guide_to_description_points_to_ask_visitor_not_explicit_request() -> None:
    """stage2 D2: "только по явной просьбе" убрано (мёртвый текст после D1 --
    гейт живёт в tool_broker, не в тексте описания); взамен -- явная отсылка
    к ask_visitor для мид-тур случая."""
    description = tool_spec("guide_to").description
    assert "только по явной просьбе" not in description.lower()
    assert "ask_visitor" in description


def test_read_only_tools_allowed_in_every_state() -> None:
    for name in (
        "list_locations",
        "list_tours",
        "estimate_route",
        "lookup_content",
        "search_content",
        "resolve_location",
    ):
        for state in range(9):
            assert is_tool_allowed(name, state)


def test_unknown_tool_never_allowed() -> None:
    assert not is_tool_allowed("does_not_exist", _S.STATE_IDLE)


def test_read_only_flag_set_for_catalog_and_content_tools() -> None:
    for name in (
        "lookup_content",
        "search_content",
        "resolve_location",
        "list_locations",
        "list_tours",
        "estimate_route",
    ):
        assert tool_spec(name).read_only is True


def test_read_only_flag_false_for_mutating_tools() -> None:
    for name in ("start_tour", "guide_to", "tell_about", "noop", "say", "ask_visitor"):
        assert tool_spec(name).read_only is False


def test_allowed_tools_idle_matches_expected_set() -> None:
    assert set(allowed_tools(_S.STATE_IDLE)) == {
        "start_tour",
        "guide_to",
        "tour_by_points",
        "tell_about",
        "say",
        "noop",
        "ask_visitor",
        "lookup_content",
        "search_content",
        "resolve_location",
        "list_locations",
        "list_tours",
        "estimate_route",
    }


def test_noop_allowed_in_every_state() -> None:
    for state in range(9):
        assert is_tool_allowed("noop", state)


def test_llm_only_hides_say_and_read_only_catalog_tools() -> None:
    visible = set(allowed_tools(_S.STATE_IDLE, llm_only=True))
    assert "say" not in visible
    assert "list_locations" not in visible
    assert "list_tours" not in visible
    assert "estimate_route" not in visible
    assert "noop" in visible
    assert "start_tour" in visible


def test_llm_only_shows_new_read_only_tools() -> None:
    visible = set(allowed_tools(_S.STATE_IDLE, llm_only=True))
    assert "lookup_content" in visible
    assert "search_content" in visible
    assert "resolve_location" in visible


def test_llm_only_false_by_default_keeps_say() -> None:
    assert "say" in allowed_tools(_S.STATE_IDLE)


def test_llm_only_still_gates_by_state() -> None:
    # guide_to -- ALL_STATES с stage2 B3 (во время тура маппится на
    # ~/redirect брокером, не отдельный гейт по состоянию).
    assert allowed_tools(_S.STATE_NARRATING, llm_only=True) == [
        "guide_to",
        "stop_tour",
        "pause",
        "noop",
        "ask_visitor",
        "lookup_content",
        "search_content",
        "resolve_location",
    ]
