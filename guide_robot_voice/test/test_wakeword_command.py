"""Разделение wake/команды без обучения, новых имён или fuzzy-совпадений."""

import pytest

from guide_robot_voice.lib.wakeword_command import command_after_wake

NAMES = ["фирая", "фирайя", "фира я", "фи рая"]


def aligned(text, starts):
    tokens, timestamps = [], []
    for word, start in zip(text.split(), starts, strict=False):
        if tokens:
            tokens.append(" ")
            timestamps.append(start)
        tokens.extend(word)
        timestamps.extend(start + i * 0.04 for i in range(len(word)))
    return tuple(tokens), tuple(timestamps)


@pytest.mark.parametrize("text", ["ферая", "фрая", "сырая", "фрея", "ферраи", "фера я"])
def test_any_spelling_before_confirmed_wake_is_not_a_command(text):
    tokens, times = aligned(text, [0.4, 0.8])
    assert command_after_wake(text, tokens, times, 1.2, NAMES) == ("", True)


def test_empty_first_result_does_not_consume_command_window():
    assert command_after_wake("", (), (), 1.2, NAMES)[0] == ""


@pytest.mark.parametrize("name", ["ферая", "фрая", "сырая"])
def test_immediate_command_survives_incorrect_name_and_overlapping_trigger(name):
    text = f"{name} расскажи интересный факт"
    tokens, times = aligned(text, [0.4, 1.04, 1.8, 2.5])
    assert command_after_wake(text, tokens, times, 1.2, NAMES) == (text, True)


def test_single_name_overlapping_trigger_waits_instead_of_calling_llm():
    tokens, times = aligned("ферайя", [1.04])
    assert command_after_wake("ферайя", tokens, times, 1.2, NAMES) == ("", True)


def test_actual_one_word_command_after_trigger_is_preserved():
    tokens, times = aligned("привет", [1.3])
    assert command_after_wake("привет", tokens, times, 1.2, NAMES) == ("привет", True)


def test_canonical_name_plus_short_command_before_late_trigger_is_preserved():
    tokens, times = aligned("фирая да", [0.4, 0.8])
    assert command_after_wake("фирая да", tokens, times, 1.2, NAMES) == ("да", True)


@pytest.mark.parametrize("times", [(1, 0.5, 0.7), (float("nan"), 0.5, 0.7), (), (0.1,)])
def test_invalid_alignment_cannot_silently_crop_command(times):
    assert command_after_wake("да!", ("д", "а", "!"), times, 1.2, NAMES) == ("да!", False)


def test_unaligned_fallback_uses_only_existing_exact_names():
    assert command_after_wake("Фирая!", (), (), 1.2, NAMES) == ("", False)
    assert command_after_wake("сырая", (), (), 1.2, NAMES) == ("сырая", False)


@pytest.mark.parametrize(
    "text,starts",
    [
        ("где выход", [0.4, 0.6]),
        ("привет ферая", [0.1, 0.6]),
        ("скажи ферая где выход", [0.1, 0.4, 0.8, 1.3]),
        ("как дела", [0.1, 0.4]),
        ("кто ты", [0.1, 0.4]),
        ("кто я", [0.1, 0.4]),
    ],
)
def test_pretrigger_command_and_name_in_middle_or_end_are_not_cropped(text, starts):
    tokens, times = aligned(text, starts)
    assert command_after_wake(text, tokens, times, 1.2, NAMES) == (text, True)
