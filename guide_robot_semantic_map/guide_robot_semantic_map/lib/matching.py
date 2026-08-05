"""Fuzzy-резолв алиасов локаций без внешних зависимостей (design.md §1.1).

Три ступени по возрастанию цены и убыванию точности:

1. точное совпадение нормализованных строк -- score 1.0, короткое замыкание;
2. совпадение по префиксу словоформы длиной >= 4 -- грубая замена
   стеммингу для русского (ASR чаще всего путает падежные окончания,
   а не начало слова: "кандинского" -> "кандинский");
3. difflib.SequenceMatcher.ratio() как добивка на всё остальное, включая
   опечатки.

score(query, candidate) -> float работает на уже нормализованных строках
(см. text_norm.normalize) и не знает про язык -- это забота resolve().
rapidfuzz дал бы лучше и быстрее, но это лишний rosdep на Orin ради
30 строк; интерфейс сохранён так, чтобы замена была локальной правкой
score() без изменения resolve().
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from guide_robot_semantic_map.lib.text_norm import normalize

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["Match", "is_confident", "resolve", "score"]

_PREFIX_MIN_LEN = 4
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_THRESHOLD = 0.6
_DEFAULT_MARGIN = 0.15


@dataclass(frozen=True)
class Match:
    """Одна локация-кандидат с итоговым скором резолва."""

    location_id: str
    score: float
    matched_alias: str


def score(query: str, candidate: str) -> float:
    """Схожесть двух уже нормализованных строк, 0..1.

    query/candidate ожидаются пропущенными через text_norm.normalize --
    функция не нормализует сама, чтобы вызывающий код (resolve) не платил
    за повторную нормализацию одного и того же алиаса на каждом сравнении.
    """
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0

    prefix_len = _shared_prefix_len(query, candidate)
    shorter, longer = sorted((len(query), len(candidate)))

    if prefix_len >= _PREFIX_MIN_LEN and prefix_len == shorter:
        # Один -- префикс другого целиком: "кандинск" в "кандинский".
        return prefix_len / longer

    ratio = SequenceMatcher(None, query, candidate).ratio()
    if prefix_len >= _PREFIX_MIN_LEN:
        # Общий префикс есть, но ни один не вложен в другой целиком
        # ("кандинский" / "кандинского") -- лёгкая подстраховка ratio,
        # не перекрывающая честные почти-совпадения.
        ratio = max(ratio, (prefix_len / longer) * 0.8)
    return ratio


def _shared_prefix_len(a: str, b: str) -> int:
    length = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        length += 1
    return length


def resolve(
    query: str,
    locations: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    language: str = "",
    max_results: int = 0,
) -> list[Match]:
    """Найти локации, чьи алиасы похожи на query.

    locations -- location_id -> {language -> [алиасы]}, как
    locations_io.Location.aliases для всех локаций сразу. language="" ищет
    по всем языкам разом. Для каждой локации в результат попадает лучший
    из её алиасов -- ResolveLocation.srv возвращает Location[], а не
    алиасы, так что дублировать локацию под каждым алиасом незачем.
    """
    normalized_query = normalize(query)
    if not normalized_query:
        return []

    matches: list[Match] = []
    for location_id, aliases_by_language in locations.items():
        candidate_aliases = _select_aliases(aliases_by_language, language)
        best_alias, best_score = "", 0.0
        for alias in candidate_aliases:
            alias_score = score(normalized_query, normalize(alias))
            if alias_score > best_score:
                best_alias, best_score = alias, alias_score
        if best_score > 0.0:
            matches.append(
                Match(location_id=location_id, score=best_score, matched_alias=best_alias)
            )

    matches.sort(key=lambda m: m.score, reverse=True)
    limit = max_results if max_results > 0 else _DEFAULT_MAX_RESULTS
    return matches[:limit]


def _select_aliases(
    aliases_by_language: Mapping[str, Sequence[str]], language: str
) -> list[str]:
    if language:
        return list(aliases_by_language.get(language, []))
    return [alias for aliases in aliases_by_language.values() for alias in aliases]


def is_confident(
    scores: Sequence[float],
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    margin: float = _DEFAULT_MARGIN,
) -> bool:
    """Достаточно ли уверенности, чтобы вести сразу, не переспрашивая (design.md §0.3).

    scores ожидается отсортированным по убыванию (как resolve() и
    отдаёт). Единственный кандидат уверен сам по себе, если прошёл порог --
    отрыва сравнивать не с чем.
    """
    if not scores or scores[0] < threshold:
        return False
    if len(scores) == 1:
        return True
    return (scores[0] - scores[1]) >= margin
