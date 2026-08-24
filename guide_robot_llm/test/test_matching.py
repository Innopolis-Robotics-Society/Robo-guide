"""match_confirm()/match_stop_phrase()/match_idle_dismiss(): чистая логика, без ROS."""

from __future__ import annotations

from guide_robot_llm.matching import (
    has_leading_wake_word,
    has_motion_intent,
    idle_turn_allowed,
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


# -- has_motion_intent: «повтори» не должно заводить моторы --


def test_motion_intent_rejects_chit_chat_and_asr_stubs() -> None:
    for text in ("привет", "здравствуй", "приятно", "повтори", "рара", "включи ва"):
        assert has_motion_intent(text) is False


def test_motion_intent_accepts_explicit_tour_or_guide() -> None:
    assert has_motion_intent("проведи экскурсию") is True
    assert has_motion_intent("начать тур") is True
    assert has_motion_intent("отведи меня в лабораторию") is True
    assert has_motion_intent("start the tour") is True


def test_motion_intent_empty_is_false() -> None:
    assert has_motion_intent("") is False
    assert has_motion_intent("   ") is False


# -- idle_turn_allowed: IDLE без «робот» не должен уходить в ЛЛМ --


def test_idle_turn_rejects_bare_chit_chat() -> None:
    assert idle_turn_allowed("привет", listen_armed=False) is False
    assert idle_turn_allowed("рара", listen_armed=False) is False
    assert idle_turn_allowed("который год музей", listen_armed=False) is False


def test_idle_turn_accepts_leading_wake_word() -> None:
    assert idle_turn_allowed("робот, привет", listen_armed=False) is True
    assert idle_turn_allowed("робот который год музей", listen_armed=False) is True


def test_idle_turn_accepts_armed_listen_window() -> None:
    assert idle_turn_allowed("привет", listen_armed=True) is True


def test_idle_turn_accepts_motion_intent_without_wake() -> None:
    assert idle_turn_allowed("проведи экскурсию", listen_armed=False) is True
    assert idle_turn_allowed("отведи меня в лабораторию", listen_armed=False) is True


def test_idle_turn_bare_wake_word_is_not_a_turn() -> None:
    assert idle_turn_allowed("робот", listen_armed=False) is False


def test_idle_turn_stop_without_wake_is_not_activation() -> None:
    assert idle_turn_allowed("стоп", listen_armed=False) is False


def test_has_leading_wake_word() -> None:
    assert has_leading_wake_word("робот, привет") is True
    assert has_leading_wake_word("привет") is False
    assert has_leading_wake_word("что такое робот") is False
