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
