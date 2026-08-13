"""Детектор дословного цитирования: длина самой длинной общей последовательности слов.

Чистая логика без rclpy (DIALOG_REWORK_PLAN.md §3.2). Ничего не блокирует
механически -- используется только как метрика в `interaction_log`, даёт
числовой ответ на вопрос «модель пересказывает или цитирует» и позволяет
ловить регресс промпта («Правило грунтования», `config/system_prompt.txt`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

__all__ = ["max_shingle_overlap"]

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def _normalize_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    normalized = _WHITESPACE.sub(" ", stripped).strip()
    return normalized.split() if normalized else []


def max_shingle_overlap(answer: str, passages: Sequence[str], *, n: int = 8) -> int:
    """Длина самой длинной общей последовательности слов между `answer` и любым из `passages`.

    Не фиксированные n-граммы Jaccard-ом, а точная длина самого длинного
    непрерывного совпадения токенов (longest common run по
    последовательности слов) -- цитирование может начаться на любом
    сдвиге, а не только на границе шинглов. `n` не участвует в подсчёте
    (совпадение ищется без фиксированного шага), зарезервирован под порог
    сравнения, который вызывающий код (`interaction_log`) применяет к
    возвращённому значению (флаг в логе при >= 8 слов подряд).
    """
    del n
    answer_words = _normalize_words(answer)
    if not answer_words:
        return 0
    best = 0
    for passage in passages:
        passage_words = _normalize_words(passage)
        best = max(best, _longest_common_run(answer_words, passage_words))
    return best


def _longest_common_run(a: list[str], b: list[str]) -> int:
    """Длина самой длинной общей непрерывной подпоследовательности `a` и `b`."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                best = max(best, curr[j])
        prev = curr
    return best
