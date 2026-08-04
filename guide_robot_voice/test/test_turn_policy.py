"""Юниты на политику конца хода."""

from __future__ import annotations

from guide_robot_voice.lib.turn_policy import (
    TurnPolicy,
    TurnPolicyConfig,
    is_syntactically_complete,
)


def make(**overrides: object) -> TurnPolicy:
    """Политика с параметрами по умолчанию из design §3.4."""
    return TurnPolicy(TurnPolicyConfig(**overrides))  # type: ignore[arg-type]


def test_long_silence_finalizes_regardless_of_text() -> None:
    """Базовый путь: тишина >= base_silence_ms финализирует даже оборванную фразу."""
    policy = make()
    assert policy.should_finalize("я хочу спросить про", 600.0, 1000.0)


def test_short_silence_with_complete_text_finalizes() -> None:
    """Быстрый путь: короткая тишина + законченная фраза."""
    policy = make()
    assert policy.should_finalize("сколько лет этому роботу", 350.0, 2000.0)


def test_short_silence_with_incomplete_text_waits() -> None:
    """Короткой тишины недостаточно, если фраза явно не закончена."""
    policy = make()
    assert not policy.should_finalize("я хочу спросить про", 350.0, 2000.0)


def test_silence_below_short_threshold_never_finalizes_on_text_alone() -> None:
    """Тишины меньше short_silence_ms не хватает, даже для законченной фразы."""
    policy = make()
    assert not policy.should_finalize("сколько лет этому роботу", 200.0, 2000.0)


def test_too_few_words_does_not_count_as_complete() -> None:
    """Одно-два слова -- недостаточно для эвристики завершённости."""
    policy = make()
    assert not policy.should_finalize("привет", 350.0, 500.0)


def test_max_utterance_forces_finalize() -> None:
    """Страховка: длинное высказывание финализируется независимо от текста и тишины."""
    policy = make(max_utterance_s=20.0)
    assert policy.should_finalize("и вот я всё говорю и говорю без остановки про", 0.0, 20000.0)


def test_no_silence_no_max_duration_waits() -> None:
    """Пока человек говорит без пауз и не уткнулись в потолок -- не финализируем."""
    policy = make()
    assert not policy.should_finalize("сколько лет этому роботу", 0.0, 3000.0)


def test_empty_text_does_not_crash_and_is_incomplete() -> None:
    """Пустой партиал (например, в начале высказывания) не роняет политику."""
    policy = make()
    assert not policy.should_finalize("", 350.0, 100.0)
    assert policy.should_finalize("", 600.0, 100.0)  # базовый путь не смотрит на текст


def test_is_syntactically_complete_rejects_trailing_preposition() -> None:
    """Прямая проверка эвристики: предлог в конце -- не конец фразы."""
    assert not is_syntactically_complete("я хочу узнать про")


def test_is_syntactically_complete_rejects_trailing_conjunction() -> None:
    """Союз в конце -- тоже не конец фразы."""
    assert not is_syntactically_complete("робот работает и")


def test_is_syntactically_complete_rejects_trailing_question_word() -> None:
    """Вопросительное слово в конце -- фраза явно не закончена."""
    assert not is_syntactically_complete("скажите пожалуйста где")


def test_is_syntactically_complete_accepts_plain_statement() -> None:
    """Обычное законченное утверждение проходит эвристику."""
    assert is_syntactically_complete("этот робот работает в музее")


def test_is_syntactically_complete_respects_min_words() -> None:
    """Параметр min_words управляет минимальной длиной."""
    assert not is_syntactically_complete("привет робот", min_words=3)
    assert is_syntactically_complete("привет большой робот", min_words=3)
