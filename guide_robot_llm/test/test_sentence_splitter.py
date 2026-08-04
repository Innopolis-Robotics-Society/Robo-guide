"""Юниты на инкрементальное клаузное разбиение."""

from __future__ import annotations

from guide_robot_llm.lib.sentence_splitter import SentenceSplitter, SentenceSplitterConfig


def make(**overrides: object) -> SentenceSplitter:
    return SentenceSplitter(SentenceSplitterConfig(**overrides))  # type: ignore[arg-type]


def feed_all(splitter: SentenceSplitter, chunks: list[str]) -> list[str]:
    clauses: list[str] = []
    for chunk in chunks:
        clauses.extend(splitter.feed(chunk))
    return clauses


def test_no_boundary_yet_returns_nothing() -> None:
    splitter = make(first_clause_min_chars=100)
    assert splitter.feed("Это начало без точки") == []


def test_sentence_boundary_needs_lookahead_char() -> None:
    """Точка в самом конце буфера не даёт клаузу -- неизвестно, что после неё."""
    splitter = make(first_clause_min_chars=100)
    assert splitter.feed("Привет.") == []
    assert splitter.feed(" Как дела") == ["Привет."]


def test_full_sentence_in_one_feed_is_split() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = splitter.feed("Первое предложение. Второе продолжается")
    assert clauses == ["Первое предложение."]


def test_question_and_exclamation_are_always_boundaries() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Как дела? ", "Отлично! ", "продолжение"])
    assert clauses == ["Как дела?", "Отлично!"]


def test_abbreviation_does_not_split() -> None:
    """'т.д.' -- не конец предложения, из списка сокращений."""
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Роботы, роботы-пылесосы и т.д. используются часто. ", "Далее"])
    assert clauses == ["Роботы, роботы-пылесосы и т.д. используются часто."]


def test_decimal_number_does_not_split() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Значение равно 3.14 в этой формуле. ", "Дальше"])
    assert clauses == ["Значение равно 3.14 в этой формуле."]


def test_initials_do_not_split() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Работу написал А. С. Пушкин в этом году. ", "Дальше"])
    assert clauses == ["Работу написал А. С. Пушкин в этом году."]


def test_semicolon_is_a_boundary() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Первая часть; ", "вторая часть"])
    assert clauses == ["Первая часть;"]


def test_forced_cut_on_max_clause_chars() -> None:
    splitter = make(max_clause_chars=20, first_clause_min_chars=100)
    long_text = "слово " * 10 + "без точек совсем"
    clauses = splitter.feed(long_text)
    assert clauses
    assert all(len(c) <= 25 for c in clauses)  # разрез по пробелу, не строго на границе
    assert "".join(clauses).replace(" ", "") in long_text.replace(" ", "")


def test_first_clause_min_chars_allows_early_cut_without_sentence_end() -> None:
    splitter = make(first_clause_min_chars=10, max_clause_chars=1000)
    clauses = splitter.feed("короткий кусок без знаков препинания продолжается дальше")
    assert clauses  # первая клауза ушла раньше конца предложения ради TTFA
    assert len(clauses[0]) >= 10


def test_first_clause_allowance_applies_only_once() -> None:
    """После первой клаузы обычные клаузы снова ждут границу предложения."""
    splitter = make(first_clause_min_chars=5, max_clause_chars=1000)
    first = splitter.feed("корот кусок ")
    assert first  # первая клауза вышла рано
    more = splitter.feed("продолжение без точки и совсем без запятых тоже длинное")
    assert more == []  # вторая клауза уже не имеет права на досрочный разрез


def test_flush_returns_remaining_buffer() -> None:
    splitter = make(first_clause_min_chars=100)
    splitter.feed("Незавершённый хвост без точки")
    assert splitter.flush() == "Незавершённый хвост без точки"


def test_flush_on_empty_buffer_returns_none() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = splitter.feed("Полное предложение. ")
    assert clauses == ["Полное предложение."]  # клауза целиком ушла через feed()
    assert splitter.flush() is None  # в буфере остался только пробел -- это не клауза


def test_flush_after_full_consumption_is_none() -> None:
    splitter = make(first_clause_min_chars=100)
    clauses = feed_all(splitter, ["Одно предложение целиком. "])
    assert clauses == ["Одно предложение целиком."]
    assert splitter.flush() is None


def test_empty_feed_does_not_crash() -> None:
    splitter = make()
    assert splitter.feed("") == []
