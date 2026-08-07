"""Нормализация текста запросов и алиасов для fuzzy-резолва (design.md §1.1).

Пайплайн: NFC (устраняет разницу между составной "й"/"ё" и базовая
буква + комбинирующий диакритик -- на входе от ASR оба варианта
равновероятны), lowercase, ё→е, схлопывание пунктуации и пробелов,
отбрасывание стоп-слов. Результат используется как ключ для точного
совпадения и как вход для остальных ступеней matching.py -- поэтому два
алиаса, отличающиеся только регистром/буквой ё/лишним пробелом, обязаны
нормализоваться в одну и ту же строку.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["STOP_WORDS", "normalize", "tokenize"]

# Служебные слова, характерные для голосовых запросов о местоположении.
# Не претендуют на полноту как список стоп-слов языка вообще -- только
# то, что реально встречается в духе "отведи к кандинскому" (design.md §1.1).
STOP_WORDS: frozenset[str] = frozenset(
    ["к", "на", "в", "у", "где", "покажи", "отведи", "хочу"]
)

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Свести текст к каноническому виду для сравнения алиасов.

    Пустой и пробельный вход даёт пустую строку -- вызывающий код решает,
    что делать с пустым запросом, это не забота нормализации.
    """
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    words = [w for w in _WHITESPACE.split(stripped) if w and w not in STOP_WORDS]
    return " ".join(words)


def tokenize(text: str) -> list[str]:
    """Нормализовать и разбить на отдельные словоформы."""
    normalized = normalize(text)
    return normalized.split() if normalized else []
