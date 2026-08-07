"""match_confirm()/match_stop_phrase(): чистая логика, без ROS (llm_plam.md §3/§9)."""

from __future__ import annotations

from guide_robot_llm.matching import match_confirm, match_stop_phrase


def test_confirm_plain_yes() -> None:
    assert match_confirm("да") is True


def test_confirm_yes_with_punctuation_and_case() -> None:
    assert match_confirm("Давай, поехали!") is True


def test_confirm_plain_no() -> None:
    assert match_confirm("нет") is False


def test_confirm_no_with_stop_word() -> None:
    assert match_confirm("нет, хватит") is False


def test_confirm_empty_text_is_unsure() -> None:
    assert match_confirm("") is None
    assert match_confirm("   ") is None


def test_confirm_unrelated_text_is_unsure() -> None:
    assert match_confirm("а что там дальше по маршруту") is None


def test_confirm_both_yes_and_no_tokens_is_unsure() -> None:
    assert match_confirm("да нет, не знаю") is None


def test_stop_phrase_hwatit() -> None:
    assert match_stop_phrase("хватит, дальше") is True


def test_stop_phrase_dalshe_alone() -> None:
    assert match_stop_phrase("дальше") is True


def test_stop_phrase_unrelated_text() -> None:
    assert match_stop_phrase("расскажи ещё про динозавров") is False


def test_stop_phrase_empty_text() -> None:
    assert match_stop_phrase("") is False
