"""validate_call(): чистая логика, без ROS."""

from __future__ import annotations

import pytest
from guide_robot_llm.tools.validate import ValidationError, validate_call


def test_tool_not_in_allowed_list_rejected() -> None:
    with pytest.raises(ValidationError, match="start_tour сейчас недоступен"):
        validate_call("start_tour", {"tour_id": "hall_a"}, tools_allowed=["stop_tour", "say"])


def test_start_tour_unknown_tour_id_rejected() -> None:
    with pytest.raises(ValidationError, match="тур"):
        validate_call(
            "start_tour",
            {"tour_id": "does_not_exist"},
            tools_allowed=["start_tour"],
            known_tour_ids=frozenset({"hall_a"}),
        )


def test_start_tour_known_tour_id_accepted() -> None:
    validate_call(
        "start_tour",
        {"tour_id": "hall_a"},
        tools_allowed=["start_tour"],
        known_tour_ids=frozenset({"hall_a"}),
    )


def test_empty_whitelist_skips_strict_membership_check() -> None:
    """known_tour_ids не подгружен вызывающим -- строгую проверку пропускаем, не рушим всё."""
    validate_call("start_tour", {"tour_id": "hall_a"}, tools_allowed=["start_tour"])


def test_guide_to_missing_location_id_rejected() -> None:
    with pytest.raises(ValidationError, match="локация"):
        validate_call("guide_to", {}, tools_allowed=["guide_to"])


def test_tour_by_points_empty_list_rejected() -> None:
    with pytest.raises(ValidationError, match="пустой список"):
        validate_call("tour_by_points", {"location_ids": []}, tools_allowed=["tour_by_points"])


def test_tour_by_points_unknown_location_rejected() -> None:
    with pytest.raises(ValidationError, match="локация"):
        validate_call(
            "tour_by_points",
            {"location_ids": ["dinosaurs", "ghost"]},
            tools_allowed=["tour_by_points"],
            known_location_ids=frozenset({"dinosaurs"}),
        )


def test_finish_answer_bad_outcome_rejected() -> None:
    with pytest.raises(ValidationError, match="outcome"):
        validate_call("finish_answer", {"outcome": 7}, tools_allowed=["finish_answer"])


def test_finish_answer_valid_outcome_accepted() -> None:
    validate_call("finish_answer", {"outcome": 1}, tools_allowed=["finish_answer"])


def test_confirm_non_bool_yes_rejected() -> None:
    with pytest.raises(ValidationError, match="yes"):
        validate_call("confirm", {"yes": "yes"}, tools_allowed=["confirm"])


def test_say_empty_text_rejected() -> None:
    with pytest.raises(ValidationError, match="пустой текст"):
        validate_call("say", {"text": "   "}, tools_allowed=["say"])


def test_say_non_empty_text_accepted() -> None:
    validate_call("say", {"text": "Секунду."}, tools_allowed=["say"])


def test_list_locations_no_args_needed() -> None:
    validate_call("list_locations", {}, tools_allowed=["list_locations"])
