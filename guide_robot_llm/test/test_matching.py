"""match_confirm()/match_stop_phrase()/match_idle_dismiss(): чистая логика, без ROS."""

from __future__ import annotations

from guide_robot_llm.matching import (
    match_confirm,
    match_idle_dismiss,
    match_stop_phrase,
    strip_wake_word,
)


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


# -- ловушки DIALOG_REWORK_PLAN.md §3.3: вопрос не должен матчиться как стоп-слово/да/нет --


def test_stop_phrase_trap_vsyo_taki_chto_eto_takoe() -> None:
    """Регрессия: «а всё-таки что это такое?» раньше матчилось как «хватит» через «всё»."""
    assert match_stop_phrase("а всё-таки что это такое?") is False


def test_stop_phrase_trap_dalshe_po_marshrutu_chto() -> None:
    assert match_stop_phrase("а что там дальше по маршруту") is False


def test_stop_phrase_trap_long_phrase_gated_by_length() -> None:
    assert match_stop_phrase("а давайте ещё немного постоим тут") is False


def test_confirm_trap_question_word_da_kogda_pridem() -> None:
    assert match_confirm("да когда мы вообще придём") is None


def test_confirm_trap_question_word_alone() -> None:
    assert match_confirm("почему") is None


def test_stop_phrase_short_confident_still_matches() -> None:
    assert match_stop_phrase("стоп") is True
    assert match_stop_phrase("закончили") is True


# -- match_idle_dismiss: IDLE-версия, живой баг "робот стоп" в IDLE --


def test_idle_dismiss_bare_stop_word() -> None:
    assert match_idle_dismiss("стоп") is True


def test_idle_dismiss_with_wake_word_prefix() -> None:
    assert match_idle_dismiss("робот стоп") is True


def test_idle_dismiss_multiple_dismiss_words() -> None:
    assert match_idle_dismiss("стоп стой") is True


def test_idle_dismiss_all_vocabulary_words() -> None:
    for word in ("стоп", "стой", "хватит", "замолчи"):
        assert match_idle_dismiss(word) is True
        assert match_idle_dismiss(f"робот {word}") is True


def test_idle_dismiss_rejects_extra_content() -> None:
    """Регрессия: лишнее содержание после стоп-слова обязано уйти к ЛЛМ, не проглатываться."""
    assert match_idle_dismiss("стоп машина") is False


def test_idle_dismiss_rejects_question() -> None:
    assert match_idle_dismiss("почему стоп") is False


def test_idle_dismiss_empty_text() -> None:
    assert match_idle_dismiss("") is False


def test_idle_dismiss_wake_word_alone_is_not_a_dismiss() -> None:
    """Одно "робот" без стоп-слова -- не команда отмены, токенов после фильтра не остаётся."""
    assert match_idle_dismiss("робот") is False


def test_idle_dismiss_unrelated_text() -> None:
    assert match_idle_dismiss("расскажи про экскурсию") is False


# -- strip_wake_word: голое wake-слово не должно порождать ход к ЛЛМ --


def test_strip_wake_word_bare_wake_word_becomes_empty() -> None:
    assert strip_wake_word("робот") == ""


def test_strip_wake_word_case_and_punctuation() -> None:
    assert strip_wake_word("Робот!") == ""


def test_strip_wake_word_repeated_wake_words() -> None:
    assert strip_wake_word("робот робот") == ""


def test_strip_wake_word_leading_prefix_is_cut() -> None:
    assert strip_wake_word("робот, отведи меня к входу") == "отведи меня к входу"
    assert strip_wake_word("Робот отведи меня к входу") == "отведи меня к входу"


def test_strip_wake_word_mid_phrase_wake_word_is_kept() -> None:
    """«робот» в середине/конце фразы -- часть содержания, не обращение."""
    assert strip_wake_word("что такое робот") == "что такое робот"


def test_strip_wake_word_prefix_of_longer_word_is_kept() -> None:
    """«роботы»/«роботов» -- не wake-слово, срезать нельзя."""
    assert strip_wake_word("роботы наступают") == "роботы наступают"


def test_strip_wake_word_empty_text() -> None:
    assert strip_wake_word("") == ""
    assert strip_wake_word("   ") == ""
