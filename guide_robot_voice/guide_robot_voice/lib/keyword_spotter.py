"""Нечёткий поиск ключевых слов в тексте по расстоянию Левенштейна.

Зачем нечёткое сравнение, а не точное. ASR партиалы -- гипотезы, не
гарантированно верный текст: "робот" на слух в шумном зале легко
становится "робот" с опечаткой на уровне фонем ("робод", "работ").
Точное совпадение потеряло бы активацию именно там, где она нужнее всего.
Расстояние Левенштейна с максимум одной правкой (fuzzy_max_distance=1
по умолчанию) ловит такие огрехи, не открывая дверь произвольным словам.

Многословные фразы ("слушай робот") сравниваются целиком, окном того же
числа слов -- вставку/пропуск слова эта схема не ловит (это уже была бы
не "лишняя буква", а другая фраза), только опечатки внутри слов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["KeywordMatch", "KeywordSpotter", "levenshtein_distance", "normalize_text"]

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """lowercase, ё->е, без пунктуации, схлопнутые пробелы."""
    text = text.lower().replace("ё", "е")
    text = _PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def levenshtein_distance(a: str, b: str) -> int:
    """Классическое динамическое программирование, без внешних зависимостей."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # удаление
                current[j - 1] + 1,  # вставка
                previous[j - 1] + cost,  # замена
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class KeywordMatch:
    """Лучшее найденное совпадение."""

    phrase: str
    distance: int
    confidence: float
    """[0..1]: 1.0 - distance / len(phrase)."""


class KeywordSpotter:
    """Ищет заданные фразы в тексте с допуском на опечатки."""

    def __init__(self, phrases: list[str], max_distance: int = 1) -> None:
        """Создать детектор. Фразы нормализуются один раз, при создании."""
        self._phrases = [normalize_text(p) for p in phrases if p.strip()]
        self._max_distance = max_distance

    def find(self, text: str) -> KeywordMatch | None:
        """Лучшее совпадение по всему тексту, или None, если ничего не прошло порог."""
        words = normalize_text(text).split()
        best: KeywordMatch | None = None
        for phrase in self._phrases:
            phrase_words = phrase.split()
            n = len(phrase_words)
            if n == 0 or n > len(words):
                continue
            for start in range(len(words) - n + 1):
                window = " ".join(words[start : start + n])
                distance = levenshtein_distance(window, phrase)
                if distance > self._max_distance:
                    continue
                confidence = 1.0 - distance / max(len(phrase), 1)
                if best is None or confidence > best.confidence:
                    best = KeywordMatch(phrase=phrase, distance=distance, confidence=confidence)
        return best
