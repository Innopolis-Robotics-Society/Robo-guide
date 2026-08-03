"""Юниты на клаузное разбиение."""

from __future__ import annotations

import pytest

from guide_robot_voice.lib.chunker import ChunkerConfig, TextChunker


@pytest.fixture
def chunker() -> TextChunker:
    """Чанкер с параметрами по умолчанию."""
    return TextChunker()


def test_spans_reconstruct_source(chunker: TextChunker) -> None:
    """Конкатенация клауз по смещениям восстанавливает исходный текст."""
    text = (
        "Перед вами робот FURo-D. Он был разработан в Южной Корее. "
        "Сейчас мы переводим его на ROS 2, это занимает примерно год."
    )
    clauses = chunker.split(text)
    assert "".join(text[c.char_start : c.char_end] for c in clauses) == text


def test_abbreviation_does_not_split(chunker: TextChunker) -> None:
    """Точка в сокращении не считается концом предложения."""
    text = "Экспонат включает двигатели, датчики и т.д. Разработка велась в Иннополисе."
    clauses = chunker.split(text)
    assert not any(c.text.endswith("и т.") for c in clauses)


def test_initials_do_not_split(chunker: TextChunker) -> None:
    """Инициалы не разрывают предложение."""
    text = "Автором концепции считается А. С. Попов, о чём говорит табличка у входа."
    clauses = chunker.split(text)
    assert len(clauses) == 1


def test_decimal_does_not_split(chunker: TextChunker) -> None:
    """Десятичная точка не разрывает предложение."""
    text = "Диаметр колеса составляет 0.15 метра, а колёсная база 0.338 метра."
    clauses = chunker.split(text)
    assert len(clauses) == 1


def test_question_always_splits(chunker: TextChunker) -> None:
    """Вопросительный знак -- граница даже перед строчной буквой.

    Проверяется на достаточно длинных фрагментах: короткие клаузы
    сознательно склеиваются обратно, см. test_short_fragments_are_merged.
    """
    text = (
        "А сколько лет этому роботу и кто именно его сюда привёз? "
        "точный год установки указан на табличке справа от постамента."
    )
    clauses = chunker.split(text)
    assert len(clauses) == 2


def test_long_text_is_bounded() -> None:
    """Длинный текст без точек режется по клаузам и не превышает max_chars."""
    config = ChunkerConfig(min_chars=20, max_chars=60)
    text = (
        "В этом зале собраны экспонаты по робототехнике, "
        "мехатронике, компьютерному зрению, обработке сигналов, "
        "а также несколько стендов по управлению приводами"
    )
    clauses = TextChunker(config).split(text)
    assert clauses
    assert all(len(c.text) <= config.max_chars for c in clauses)


def test_short_fragments_are_merged() -> None:
    """Короткие предложения склеиваются: иначе просодия рвётся."""
    config = ChunkerConfig(min_chars=40, max_chars=160)
    text = "Да. Нет. Возможно. Этот экспонат появился здесь совсем недавно."
    clauses = TextChunker(config).split(text)
    assert len(clauses) < 4


def test_terminal_flag(chunker: TextChunker) -> None:
    """Флаг terminal отражает наличие финальной пунктуации."""
    clauses = chunker.split("Это очень старый экспонат нашего музея робототехники.")
    assert clauses[-1].terminal


def test_empty_input(chunker: TextChunker) -> None:
    """Пустой и пробельный вход дают пустой список."""
    assert chunker.split("") == []
    assert chunker.split("   \n  ") == []


def test_unbreakable_token_survives() -> None:
    """Одно длинное слово не режется посередине."""
    config = ChunkerConfig(min_chars=5, max_chars=20)
    text = "https://example.org/very/long/path/that/cannot/be/split"
    clauses = TextChunker(config).split(text)
    assert len(clauses) == 1
