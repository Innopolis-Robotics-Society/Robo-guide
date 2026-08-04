"""Юниты на нечёткий поиск ключевых слов."""

from __future__ import annotations

from guide_robot_voice.lib.keyword_spotter import (
    KeywordSpotter,
    levenshtein_distance,
    normalize_text,
)


def test_normalize_lowercases_and_strips_punctuation() -> None:
    """Пунктуация и регистр не должны мешать сравнению."""
    assert normalize_text("Робот, слушай!") == "робот слушай"


def test_normalize_replaces_yo_with_ye() -> None:
    """ASR может отдавать и ё, и е -- сравнение не должно от этого зависеть."""
    assert normalize_text("замолчём") == normalize_text("замолчем")


def test_levenshtein_identical_strings() -> None:
    """Расстояние между одинаковыми строками -- ноль."""
    assert levenshtein_distance("робот", "робот") == 0


def test_levenshtein_one_substitution() -> None:
    """Одна замена буквы -- расстояние 1."""
    assert levenshtein_distance("робот", "робод") == 1


def test_levenshtein_against_empty() -> None:
    """Расстояние до пустой строки -- её длина."""
    assert levenshtein_distance("стоп", "") == 4
    assert levenshtein_distance("", "стоп") == 4


def test_exact_match_found() -> None:
    """Точное совпадение находится с расстоянием 0."""
    spotter = KeywordSpotter(["робот"], max_distance=1)
    match = spotter.find("привет робот как дела")
    assert match is not None
    assert match.phrase == "робот"
    assert match.distance == 0
    assert match.confidence == 1.0


def test_fuzzy_match_within_distance_is_found() -> None:
    """Опечатка в пределах max_distance -- совпадение находится."""
    spotter = KeywordSpotter(["робот"], max_distance=1)
    match = spotter.find("привет робод как дела")
    assert match is not None
    assert match.distance == 1


def test_match_beyond_max_distance_is_rejected() -> None:
    """Слишком большая опечатка -- совпадения нет."""
    spotter = KeywordSpotter(["робот"], max_distance=1)
    assert spotter.find("привет рлбдт как дела") is None


def test_multiword_phrase_matches_as_window() -> None:
    """Многословная фраза сравнивается окном того же числа слов."""
    spotter = KeywordSpotter(["слушай робот"], max_distance=1)
    match = spotter.find("эй слушай робот пожалуйста")
    assert match is not None
    assert match.phrase == "слушай робот"


def test_no_match_returns_none() -> None:
    """Полностью не связанный текст -- None, а не случайное совпадение."""
    spotter = KeywordSpotter(["робот"], max_distance=1)
    assert spotter.find("сегодня хорошая погода") is None


def test_empty_text_returns_none() -> None:
    """Пустой партиал (начало высказывания) не роняет поиск."""
    spotter = KeywordSpotter(["робот"], max_distance=1)
    assert spotter.find("") is None


def test_best_match_wins_among_several_phrases() -> None:
    """Из нескольких фраз побеждает та, что ближе (выше confidence)."""
    spotter = KeywordSpotter(["стоп", "стой"], max_distance=1)
    match = spotter.find("пожалуйста стой")
    assert match is not None
    assert match.phrase == "стой"
    assert match.distance == 0


def test_phrase_longer_than_text_is_not_matched() -> None:
    """Фраза длиннее текста по словам не может совпасть -- без IndexError."""
    spotter = KeywordSpotter(["слушай меня робот пожалуйста"], max_distance=1)
    assert spotter.find("робот") is None
