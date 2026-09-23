"""match_confirm()/match_stop_phrase()/match_idle_dismiss(): чистая логика, без ROS."""

from __future__ import annotations

from guide_robot_llm.matching import (
    focus_on_address,
    has_leading_wake_word,
    has_motion_intent,
    idle_turn_allowed,
    is_wake_keyword,
    looks_like_chit_chat,
    match_confirm,
    match_end_tour,
    match_idle_dismiss,
    match_start_tour,
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


# -- stage2 C3, живой баг 2.4: составные фразы с союзом не схлопываются в "да" --


def test_confirm_compound_phrase_with_conjunction_is_unsure() -> None:
    assert match_confirm("хорошо но сначала туалет") is None


def test_confirm_no_davai_is_unsure() -> None:
    assert match_confirm("не, давай") is None


def test_confirm_nu_net_is_confident_no() -> None:
    assert match_confirm("ну нет") is False


def test_confirm_plain_da_still_confident_yes() -> None:
    assert match_confirm("да") is True


def test_confirm_yes_word_with_short_conjunction_is_unsure() -> None:
    """Живой баг 2.4 в его самой опасной форме: короткая реплика (<= 3 токена),
    где голое пересечение множеств раньше давало уверенное "да"."""
    assert match_confirm("хорошо но подожди") is None


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


def test_end_tour_stop_ekskursiya() -> None:
    """Живой баг: «стоп экскурсия» уходило в SKIP_STOP и ехало на следующую точку."""
    assert match_end_tour("стоп экскурсия") is True
    assert match_end_tour("фирая стоп экскурсия") is True
    assert match_end_tour("стоп останови экскурсию") is True
    assert match_stop_phrase("стоп экскурсия") is False


def test_start_tour_phrase() -> None:
    assert match_start_tour("начни экскурсию") is True
    assert match_start_tour("фирая начни экскурсию") is True
    assert match_start_tour("проведи экскурсию") is True
    assert match_start_tour("начать тур") is True
    assert match_start_tour("что такое экскурсия") is False
    assert match_start_tour("стоп экскурсия") is False
    assert match_start_tour("вернись домой") is False
    assert match_start_tour("привет") is False
    assert match_end_tour("вернись домой") is True
    assert match_end_tour("фирая вернись домой") is True
    assert match_end_tour("едем домой") is True
    assert match_stop_phrase("вернись домой") is False
    assert match_end_tour("сколько будет семь") is False
    assert match_end_tour("хватит, дальше") is False
    assert match_stop_phrase("хватит, дальше") is True
    assert match_end_tour("стоп") is False


# -- match_idle_dismiss: IDLE-версия, живой баг "робот стоп" (прежнее имя) в IDLE --


def test_idle_dismiss_bare_stop_word() -> None:
    assert match_idle_dismiss("стоп") is True


def test_idle_dismiss_with_wake_word_prefix() -> None:
    assert match_idle_dismiss("фирая стоп") is True
    assert match_idle_dismiss("Фирая, стоп") is True
    assert match_idle_dismiss("фира я стоп") is True


def test_idle_dismiss_multiple_dismiss_words() -> None:
    assert match_idle_dismiss("стоп стой") is True


def test_idle_dismiss_all_vocabulary_words() -> None:
    for word in ("стоп", "стой", "хватит", "замолчи"):
        assert match_idle_dismiss(word) is True
        assert match_idle_dismiss(f"фирая {word}") is True


def test_idle_dismiss_rejects_extra_content() -> None:
    """Регрессия: лишнее содержание после стоп-слова обязано уйти к ЛЛМ, не проглатываться."""
    assert match_idle_dismiss("стоп машина") is False


def test_idle_dismiss_rejects_question() -> None:
    assert match_idle_dismiss("почему стоп") is False


def test_idle_dismiss_empty_text() -> None:
    assert match_idle_dismiss("") is False


def test_idle_dismiss_wake_word_alone_is_not_a_dismiss() -> None:
    """Одно "фирая" без стоп-слова -- не команда отмены, токенов после фильтра не остаётся."""
    assert match_idle_dismiss("фирая") is False


def test_idle_dismiss_unrelated_text() -> None:
    assert match_idle_dismiss("расскажи про экскурсию") is False


# -- strip_wake_word: голое wake-слово не должно порождать ход к ЛЛМ --


def test_strip_wake_word_bare_wake_word_becomes_empty() -> None:
    assert strip_wake_word("фирая") == ""


def test_strip_wake_word_case_and_punctuation() -> None:
    assert strip_wake_word("Фирая!") == ""


def test_strip_wake_word_repeated_wake_words() -> None:
    assert strip_wake_word("фирая фирая") == ""
    assert strip_wake_word("эй фирая, фирая") == ""


def test_strip_wake_word_leading_prefix_is_cut() -> None:
    assert strip_wake_word("фирая, отведи меня к входу") == "отведи меня к входу"
    assert strip_wake_word("Фирая отведи меня к входу") == "отведи меня к входу"
    assert strip_wake_word("слушай фирая, отведи меня к входу") == "отведи меня к входу"


def test_strip_wake_word_mid_phrase_wake_word_is_kept() -> None:
    """Имя в середине/конце фразы -- часть содержания, не обращение."""
    assert strip_wake_word("как тебя зовут фирая") == "как тебя зовут фирая"


def test_strip_wake_word_prefix_of_longer_word_is_kept() -> None:
    """Слово, начинающееся как имя, но длиннее -- не wake-слово, срезать нельзя."""
    assert strip_wake_word("фираянка пришла") == "фираянка пришла"
    assert strip_wake_word("вера я хочу к лидару") == "вера я хочу к лидару"


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


# -- idle_turn_allowed: IDLE без «фирая» не должен уходить в ЛЛМ --


def test_idle_turn_rejects_bare_chit_chat() -> None:
    assert idle_turn_allowed("привет", listen_armed=False) is False
    assert idle_turn_allowed("рара", listen_armed=False) is False
    assert idle_turn_allowed("который год музей", listen_armed=False) is False


def test_idle_turn_accepts_leading_wake_word() -> None:
    assert idle_turn_allowed("фирая, привет", listen_armed=False) is True
    assert idle_turn_allowed("фирая который год музей", listen_armed=False) is True
    assert idle_turn_allowed("ферая привет", listen_armed=False) is True


def test_idle_turn_accepts_armed_listen_window() -> None:
    assert idle_turn_allowed("привет", listen_armed=True) is True


def test_idle_turn_rejects_motion_intent_without_wake() -> None:
    assert idle_turn_allowed("проведи экскурсию", listen_armed=False) is False
    assert idle_turn_allowed("отведи меня в лабораторию", listen_armed=False) is False


def test_idle_turn_bare_wake_word_is_not_a_turn() -> None:
    assert idle_turn_allowed("фирая", listen_armed=False) is False


def test_idle_turn_stop_without_wake_is_not_activation() -> None:
    assert idle_turn_allowed("стоп", listen_armed=False) is False


def test_has_leading_wake_word() -> None:
    assert has_leading_wake_word("фирая, привет") is True
    assert has_leading_wake_word("привет") is False
    assert has_leading_wake_word("как тебя зовут фирая") is False
    assert has_leading_wake_word("робот, привет") is False


# -- варианты записи имени GigaAM: лексика voice/config/voice*.yaml activation_phrases --


def test_strip_wake_word_asr_variants_of_the_name() -> None:
    """Все варианты из activation_phrases wakeword_node и типичные ошибки ASR режутся."""
    for heard in (
        "фирая",
        "фирайя",
        "фирае",
        "фираю",
        "ферая",
        "фира я",
        "фи рая",
        "эй фирая",
        "слушай фирая",
        "Эй, Фирая!",
    ):
        assert strip_wake_word(heard) == "", heard
        assert strip_wake_word(f"{heard} привет") == "привет", heard
        assert idle_turn_allowed(f"{heard} привет", listen_armed=False) is True, heard


def test_is_wake_keyword_matches_activation_phrases_not_stop_words() -> None:
    """`Wakeword.keyword` от wakeword_node: имя открывает окно, стоп-слова -- нет."""
    for phrase in ("фирая", "фирайя", "фира я", "фи рая", "эй фирая", "слушай фирая"):
        assert is_wake_keyword(phrase) is True, phrase
    for phrase in ("стоп", "стой", "хватит", "замолчи", "робот", "", "  "):
        assert is_wake_keyword(phrase) is False, phrase


def test_looks_like_chit_chat_greetings() -> None:
    assert looks_like_chit_chat("привет") is True
    assert looks_like_chit_chat("как дела") is True
    assert looks_like_chit_chat("проведи к кафе") is False
    assert looks_like_chit_chat("отведи в лабораторию") is False


# -- focus_on_address: слитная речь до «фирая» не идёт в ЛЛМ --


def test_focus_cuts_speech_before_mid_phrase_address() -> None:
    heard = "ну и вот мы вчера ходили в кафе фирая отведи меня к входу"
    assert focus_on_address(heard) == "фирая отведи меня к входу"
    assert strip_wake_word(focus_on_address(heard)) == "отведи меня к входу"


def test_focus_keeps_address_prefix() -> None:
    heard = "да ладно тебе эй фирая, что такое лидар"
    assert focus_on_address(heard) == "эй фирая, что такое лидар"


def test_focus_uses_last_address_with_request() -> None:
    heard = "фирая подожди, фирая расскажи про сонары"
    assert focus_on_address(heard) == "фирая расскажи про сонары"


def test_focus_keeps_trailing_name_without_request() -> None:
    assert focus_on_address("как тебя зовут, фирая?") == "как тебя зовут, фирая?"
    assert focus_on_address("фирая, как дела, фирая") == "фирая, как дела, фирая"


def test_focus_without_address_is_identity() -> None:
    assert focus_on_address("отведи меня в лабораторию") == "отведи меня в лабораторию"
    assert focus_on_address("") == ""


def test_focus_ignores_name_inside_other_word() -> None:
    assert focus_on_address("в эфирая программа идёт") == "в эфирая программа идёт"


def test_mid_phrase_address_allows_turn_without_armed_listen() -> None:
    """Партиал с «фирая» могли пропустить -- обращение в финале всё равно считается."""
    heard = focus_on_address("мы тут болтали фирая который час")
    assert idle_turn_allowed(heard, listen_armed=False) is True


def test_wake_variants_misheard_by_asr() -> None:
    """GigaAM не знает имени: «ферая», «феррай», «феррари», «фираеи» -- обращение."""
    for heard in ("ферая", "феррай", "феррая", "феррари", "фираеи", "фираей", "фи рая"):
        assert is_wake_keyword(heard), heard
        assert strip_wake_word(f"{heard} расскажи анекдот") == "расскажи анекдот", heard
    heard = "ой она же не робот она же феррари феррари расскажи анекдот"
    assert strip_wake_word(focus_on_address(heard)) == "расскажи анекдот"


def test_wake_variants_do_not_eat_ordinary_words() -> None:
    for text in ("фирма работает", "вчера я приехал", "ферма у дороги", "фираянка пришла"):
        assert strip_wake_word(text) == text, text
