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


def test_start_tour_from_povtori_rejected() -> None:
    """Живой баг: «повтори» в IDLE стало start_tour lab_demo, робот поехал."""
    with pytest.raises(ValidationError, match="явной просьбе"):
        validate_call(
            "start_tour",
            {"tour_id": "lab_demo"},
            tools_allowed=["start_tour"],
            known_tour_ids=frozenset({"lab_demo"}),
            user_text="повтори",
        )


def test_start_tour_from_privet_rejected() -> None:
    with pytest.raises(ValidationError, match="явной просьбе"):
        validate_call(
            "start_tour",
            {"tour_id": "lab_demo"},
            tools_allowed=["start_tour"],
            user_text="привет",
        )


def test_start_tour_explicit_excursion_accepted() -> None:
    validate_call(
        "start_tour",
        {"tour_id": "lab_demo"},
        tools_allowed=["start_tour"],
        known_tour_ids=frozenset({"lab_demo"}),
        user_text="проведи экскурсию по лаборатории",
    )


def test_guide_to_without_user_text_stays_programmatic() -> None:
    """Скрипт/тест без реплики -- гейт не трогает, моторы можно завести руками."""
    validate_call("guide_to", {"location_id": "lab105a"}, tools_allowed=["guide_to"])


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


def test_lookup_content_missing_content_id_rejected() -> None:
    with pytest.raises(ValidationError, match="content_id"):
        validate_call("lookup_content", {}, tools_allowed=["lookup_content"])


def test_lookup_content_bad_mode_rejected() -> None:
    with pytest.raises(ValidationError, match="mode"):
        validate_call(
            "lookup_content",
            {"content_id": "robo_guide", "mode": "long"},
            tools_allowed=["lookup_content"],
        )


def test_lookup_content_valid_accepted() -> None:
    validate_call(
        "lookup_content", {"content_id": "robo_guide"}, tools_allowed=["lookup_content"]
    )


def test_search_content_empty_query_rejected() -> None:
    with pytest.raises(ValidationError, match="query"):
        validate_call("search_content", {"query": "  "}, tools_allowed=["search_content"])


def test_search_content_valid_accepted() -> None:
    validate_call(
        "search_content", {"query": "сколько весит робот"}, tools_allowed=["search_content"]
    )


def test_resolve_location_empty_query_rejected() -> None:
    with pytest.raises(ValidationError, match="query"):
        validate_call("resolve_location", {}, tools_allowed=["resolve_location"])


def test_resolve_location_valid_accepted() -> None:
    validate_call(
        "resolve_location", {"query": "лидар"}, tools_allowed=["resolve_location"]
    )


def test_ask_visitor_empty_question_rejected() -> None:
    with pytest.raises(ValidationError, match="question"):
        validate_call(
            "ask_visitor",
            {"question": "  ", "on_yes": {"tool": "noop", "args": {}}, "on_no": ""},
            tools_allowed=["ask_visitor", "noop"],
        )


def test_ask_visitor_missing_on_yes_rejected() -> None:
    with pytest.raises(ValidationError, match="on_yes"):
        validate_call(
            "ask_visitor",
            {"question": "Прервать экскурсию?"},
            tools_allowed=["ask_visitor"],
        )


def test_ask_visitor_on_yes_tool_not_allowed_rejected() -> None:
    """on_yes.tool гоняется через обычный validate_call -- недоступный в
    текущем состоянии инструмент режется тем же гейтом, что и прямой вызов."""
    with pytest.raises(ValidationError, match="guide_to сейчас недоступен"):
        validate_call(
            "ask_visitor",
            {
                "question": "Прервать экскурсию и пойти к лидару?",
                "on_yes": {"tool": "guide_to", "args": {"location_id": "livox_mid70"}},
                "on_no": "Хорошо, продолжаем.",
            },
            tools_allowed=["ask_visitor"],  # guide_to НЕ в списке
        )


def test_ask_visitor_on_yes_recursion_into_ask_visitor_rejected() -> None:
    with pytest.raises(ValidationError, match="ask_visitor"):
        validate_call(
            "ask_visitor",
            {
                "question": "Точно?",
                "on_yes": {"tool": "ask_visitor", "args": {}},
                "on_no": "",
            },
            tools_allowed=["ask_visitor"],
        )


def test_ask_visitor_on_no_must_be_string() -> None:
    with pytest.raises(ValidationError, match="on_no"):
        validate_call(
            "ask_visitor",
            {
                "question": "Прервать экскурсию?",
                "on_yes": {"tool": "noop", "args": {}},
                "on_no": None,
            },
            tools_allowed=["ask_visitor", "noop"],
        )


def test_ask_visitor_on_yes_motion_intent_gate_not_applied_to_user_text() -> None:
    """stage2 D2 golden case: «хочу посмотреть промобот» не содержит явной просьбы
    ехать -- but on_yes=guide_to обязан пройти, подтверждение придёт отдельным
    ходом («да»), а не из этой реплики (C1: user_text в рекурсию не идёт)."""
    validate_call(
        "ask_visitor",
        {
            "question": "Прервать экскурсию и поехать к промоботу?",
            "on_yes": {"tool": "guide_to", "args": {"location_id": "promobot_m13_artist"}},
            "on_no": "Хорошо, продолжаем.",
        },
        tools_allowed=["ask_visitor", "guide_to"],
        known_location_ids=frozenset({"promobot_m13_artist"}),
        user_text="хочу посмотреть промобот",
    )


def test_ask_visitor_valid_accepted() -> None:
    validate_call(
        "ask_visitor",
        {
            "question": "Прервать экскурсию?",
            "on_yes": {"tool": "noop", "args": {}},
            "on_no": "Хорошо, продолжаем.",
        },
        tools_allowed=["ask_visitor", "noop"],
    )
