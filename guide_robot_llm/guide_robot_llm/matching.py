"""ASR-фраза -> да/нет/стоп-слово, локально, без ЛЛМ (llm_plam.md §3/§9).

Быстрый путь мимо ЛЛМ для `confirm` ("Идём дальше?") и для "хватит,
дальше" в `ANSWERING` -- риск §9 плана: суммарная латентность ASR -> ЛЛМ
-> `submit_confirm` рискует превысить терпение посетителя (>3 с
посетитель повторит ответ). Здесь -- локальное word-level сопоставление по
нормализованному тексту, без внешней модели; неуверенный случай -- `None`/
`False`, вызывающий код (`tool_broker_node.py`, шаг 5 -- ЛЛМ) решает сам.

Нормализация -- тот же пайплайн, что `guide_robot_semantic_map/lib/
text_norm.py` (NFC, lowercase, ё->е, схлопывание пунктуации), скопирован,
не импортирован -- пакет умышленно не зависит от guide_robot_semantic_map
в рантайме (тот же принцип, что `lib/qos.py`: несовпадающая копия хуже
молчаливого рантайм-импорта чужого пакета только ради одной функции).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["match_confirm", "match_stop_phrase"]

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_YES_WORDS = frozenset(
    {
        "да",
        "давай",
        "давайте",
        "поехали",
        "конечно",
        "хорошо",
        "ага",
        "угу",
        "продолжай",
        "продолжайте",
        "идем",
        "идём",
    }
)
_NO_WORDS = frozenset({"нет", "не", "хватит", "стоп", "достаточно"})
_STOP_WORDS = frozenset({"хватит", "дальше", "стоп", "достаточно", "закончили", "все", "всё"})


def _tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    normalized = _WHITESPACE.sub(" ", stripped).strip()
    return set(normalized.split()) if normalized else set()


def match_confirm(text: str) -> bool | None:
    """Разобрать ответ на «Идём дальше?». `None` -- неуверенно, передать ЛЛМ."""
    tokens = _tokens(text)
    has_yes = bool(tokens & _YES_WORDS)
    has_no = bool(tokens & _NO_WORDS)
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


def match_stop_phrase(text: str) -> bool:
    """Проверить, значит ли фраза уверенно «хватит, дальше» (ANSWERING -> SKIP_STOP)."""
    return bool(_tokens(text) & _STOP_WORDS)
