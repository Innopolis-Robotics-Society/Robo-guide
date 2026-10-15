"""Отделить акустически подтверждённое имя от команды по CTC таймингам.

Это НЕ классификатор wakeword и не расширение допустимых слов KWS.
Обрабатывается только первая реплика после уже принятого acoustic wake.
"""

from __future__ import annotations

import math
import re
from itertools import pairwise

from guide_robot_voice.lib.keyword_spotter import normalize_text

_SPLIT_FRAGMENT_WORDS = 2
_MIN_SPLIT_STEM_LETTERS = 4
_MAX_FRAGMENT_LETTERS = 7
_WAKE_NAME = "фирая"
_WAKE_ANCHOR = "р"
_TRIGGER_LAG_S = 0.16
_UNALIGNED_WORDS = 3
_VOWELS = frozenset("аеиоуыэюяй")
# Глухие шумные и «в»: GigaAM путает именно начальный согласный имени.
_NOISY_CONSONANTS = frozenset("фвхчтпсшкц")


def _substitution_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if (a in _VOWELS and b in _VOWELS) or (a in _NOISY_CONSONANTS and b in _NOISY_CONSONANTS):
        return 0.5
    return 1.0


def name_distance(word: str) -> float:
    """Взвешенный Левенштейн до имени; близкие по звучанию замены стоят 0,5."""
    word = re.sub(r"(.)\1+", r"\1", normalize_text(word).replace(" ", ""))
    row = [float(i) for i in range(len(_WAKE_NAME) + 1)]
    for i, a in enumerate(word, 1):
        previous, row = row, [float(i)]
        for j, b in enumerate(_WAKE_NAME, 1):
            row.append(
                min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + _substitution_cost(a, b))
            )
    return row[-1]


def wake_name_confirmed(
    text: str,
    tokens: tuple[str, ...],
    timestamps: tuple[float, ...],
    wake_offset: float,
    max_distance: float,
) -> bool:
    """Вето акустического wake: до trigger в расшифровке должно стоять имя.

    KWS-модель срабатывает и на шум, и на чужую речь. Это только отказ:
    слово, похожее на имя, само окно не открывает -- нужен принятый кандидат.
    Проверяются слова, начатые до trigger, и соседние пары («фира я»).
    Пустая расшифровка означает, что речи у trigger не было.
    """
    joined = "".join(tokens)
    words = [normalize_text(word) for word in text.split()]
    if (
        tokens
        and len(tokens) == len(timestamps)
        and joined.strip().casefold() == text.strip().casefold()
        and math.isfinite(wake_offset)
        and all(math.isfinite(stamp) for stamp in timestamps)
    ):
        char_times = [
            stamp for token, stamp in zip(tokens, timestamps, strict=False) for _ in token
        ]
        words = [
            normalize_text(match.group())
            for match in re.finditer(r"\S+", joined)
            if char_times[match.start()] <= wake_offset + _TRIGGER_LAG_S
        ]
    else:
        words = words[:_UNALIGNED_WORDS]
    candidates = [*words, *(a + b for a, b in pairwise(words))]
    return any(
        _WAKE_ANCHOR in candidate and name_distance(candidate) <= max_distance
        for candidate in candidates
    )


def command_after_wake(
    text: str,
    tokens: tuple[str, ...],
    timestamps: tuple[float, ...],
    wake_offset: float,
    fallback_names: list[str],
) -> tuple[str, bool]:
    """Вернуть (команда, использованы тайминги); пусто означает ждать команду.

    sherpa nemo_ctc отдаёт время эмиссии символов (НЕ forced alignment).
    Короткий фрагмент у trigger не считается самостоятельной командой,
    независимо от написания имени. НЕ отрезаем все токены до KWS: детектор
    запаздывает, и в pre-roll уже может находиться начало настоящей команды.
    Многословную фразу сохраняем целиком, включая имя в середине/конце.
    Каноническое ведущее имя удаляется только по прежнему точному списку.
    Без валидных таймингов используется прежний точный список, не fuzzy.
    """
    text = text.strip()
    normalized = normalize_text(text)
    names = sorted({normalize_text(name) for name in fallback_names}, key=len, reverse=True)
    canonical_tail = None
    for name in names:
        if normalized == name:
            canonical_tail = ""
            break
        if normalized.startswith(name + " "):
            canonical_tail = normalized[len(name) :].strip()
            break
    if not text:
        return "", False
    joined = "".join(tokens)
    valid = (
        bool(tokens)
        and len(tokens) == len(timestamps)
        and joined.strip().casefold() == text.casefold()
        and math.isfinite(wake_offset)
        and wake_offset >= 0
        and all(math.isfinite(stamp) and stamp >= 0 for stamp in timestamps)
        and all(a <= b for a, b in pairwise(timestamps))
    )
    if not valid:
        return (canonical_tail if canonical_tail is not None else text), False
    char_times = [stamp for token, stamp in zip(tokens, timestamps, strict=False) for _ in token]
    words = list(re.finditer(r"\S+", joined))
    if not words:
        return "", True
    if canonical_tail is not None:
        return canonical_tail, True
    pieces = [normalize_text(word.group()) for word in words]
    letters = sum(len(piece) for piece in pieces)
    # Одна короткая эмиссия или имя, ошибочно разбитое на слоги: «фера я».
    # Это локальная эвристика уже admitted wake-фрагмента, не лексика KWS.
    split_fragment = (
        len(words) == _SPLIT_FRAGMENT_WORDS
        and len(pieces[0]) >= _MIN_SPLIT_STEM_LETTERS
        and len(pieces[1]) == 1
    )
    short_fragment = 1 <= letters <= _MAX_FRAGMENT_LETTERS and (len(words) == 1 or split_fragment)
    if (
        short_fragment
        and char_times[words[0].start()] < wake_offset
        and char_times[words[-1].end() - 1] <= wake_offset + 0.16
    ):
        return "", True
    return text, True
