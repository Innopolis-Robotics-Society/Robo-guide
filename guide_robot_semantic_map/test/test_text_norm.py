"""Юниты на нормализацию текста алиасов/запросов."""

from __future__ import annotations

import unicodedata

from guide_robot_semantic_map.lib.text_norm import normalize, tokenize


def test_lowercases() -> None:
    assert normalize("Кандинский") == "кандинский"


def test_yo_folds_to_ye() -> None:
    assert normalize("Ёлка") == "елка"
    assert normalize("елка") == "елка"


def test_nfd_input_normalizes_same_as_nfc() -> None:
    nfc = "ёлка"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd  # разное байтовое представление на входе
    assert normalize(nfc) == normalize(nfd)


def test_punctuation_collapses_to_space() -> None:
    assert normalize("Кандинский, Композиция №8!") == "кандинский композиция 8"


def test_multiple_spaces_collapse() -> None:
    assert normalize("главный   вход") == "главный вход"


def test_strips_leading_trailing_whitespace() -> None:
    assert normalize("  вход  ") == "вход"


def test_drops_stop_words() -> None:
    assert normalize("отведи к кандинскому") == "кандинскому"
    assert normalize("где касса") == "касса"
    assert normalize("хочу на выставку") == "выставку"


def test_empty_and_whitespace_input() -> None:
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_only_stop_words_yields_empty() -> None:
    assert normalize("где") == ""
    assert normalize("покажи в") == ""


def test_tokenize_splits_words() -> None:
    assert tokenize("Композиция VIII") == ["композиция", "viii"]


def test_tokenize_empty_input() -> None:
    assert tokenize("") == []
    assert tokenize("где") == []


def test_english_passthrough() -> None:
    assert normalize("Kandinsky, Composition Eight") == "kandinsky composition eight"
