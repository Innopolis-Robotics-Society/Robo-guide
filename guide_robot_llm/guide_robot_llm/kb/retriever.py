"""BM25-поиск по корпусу пассажей экскурсовода (DIALOG_REWORK_PLAN.md §3.2).

Чистая логика без rclpy -- `dialog_agent_node.py` строит `BM25Retriever` один
раз на `on_activate` (`from_jsonl`) и дальше только зовёт `search()` на
каждый ход. Эмбеддинги в v1 умышленно не берём: корпус маленький, а лишняя
модель стоит VRAM, которой на Jetson нет (см. `DIALOG_REWORK_PLAN.md §11`).

Нормализация запроса и корпуса -- тот же пайплайн NFC/lower/ё->е/схлопывание
пунктуации, что `matching.py`, плюс грубый стрип русских окончаний и список
стоп-слов: без стемминга «экскурсии» в вопросе не находит «экскурсия» в
корпусе, а частотные служебные слова иначе доминируют в скоринге коротких
пассажей. BM25 -- `kb/bm25.py`, без pip.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from guide_robot_llm.kb.bm25 import BM25Okapi
from guide_robot_llm.kb.chunker import Passage

__all__ = ["BM25Retriever"]

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_DEFAULT_LOCATION_BOOST = 1.5

# Частотные русские служебные слова (предлоги/союзы/частицы/местоимения) --
# без них короткие пассажи ранжируются в основном по совпадению "и"/"в"/"на".
_STOPWORDS = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы
    за бы по только ее мне было вот от меня еще нет о из ему теперь когда
    даже ну вдруг ли если или ни быть был него до вас нибудь опять уж вам
    ведь там потом себя ничего ей может они тут где есть надо ней для мы
    тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж
    тогда кто этот того потому этого какой совсем ним здесь этом один
    почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при
    наконец два об другой хоть после над больше тот через эти нас про
    всего них какая много разве три эту моя впрочем свою этой перед
    иногда лучше чуть том нельзя такой им более всегда конечно всю между
    """.split()
)

# Длинные сначала: «лаборатории» -> лаборатор, «лаборатория» -> лаборатор.
_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ией",
    "иям",
    "иях",
    "ого",
    "ему",
    "ыми",
    "ими",
    "ием",
    "ии",
    "ия",
    "ья",
    "ию",
    "ью",
    "ов",
    "ев",
    "ах",
    "ях",
    "ам",
    "ям",
    "ом",
    "ем",
    "ой",
    "ый",
    "ий",
    "ая",
    "ое",
    "ее",
    "ые",
    "ие",
    "ую",
    "а",
    "я",
    "у",
    "ю",
    "е",
    "о",
    "и",
    "ы",
    "ь",
)


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _normalize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    normalized = _WHITESPACE.sub(" ", stripped).strip()
    return [_stem(word) for word in normalized.split() if word not in _STOPWORDS]


@dataclass(frozen=True)
class _Entry:
    passage: Passage
    tokens: list[str]


class BM25Retriever:
    """BM25 поверх заранее собранного корпуса `Passage` (`kb/chunker.py`/`scripts/build_kb.py`)."""

    def __init__(
        self, passages: Sequence[Passage], *, location_boost: float = _DEFAULT_LOCATION_BOOST
    ) -> None:
        """Проиндексировать `passages`. Пустой корпус -- валидный случай: `search()` вернёт []."""
        self._location_boost = location_boost
        self._entries = [
            _Entry(passage=p, tokens=_normalize(f"{p.heading} {p.text}")) for p in passages
        ]
        corpus_tokens = [entry.tokens for entry in self._entries]
        self._bm25 = BM25Okapi(corpus_tokens) if corpus_tokens else None

    @property
    def passages(self) -> tuple[Passage, ...]:
        """Все проиндексированные пассажи -- для вспомогательных индексов вызывающего кода.

        Например, `location_id -> [passage_id, ...]` для `boost_ids` в `search()`.
        """
        return tuple(entry.passage for entry in self._entries)

    @classmethod
    def from_jsonl(
        cls, path: str, *, location_boost: float = _DEFAULT_LOCATION_BOOST
    ) -> BM25Retriever:
        """Собрать ретривер из `kb.jsonl` (одна `Passage` на строку, см. `scripts/build_kb.py`)."""
        passages: list[Passage] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                passages.append(
                    Passage(
                        id=data["id"],
                        heading=data["heading"],
                        text=data["text"],
                        location_ids=tuple(data.get("location_ids", ())),
                    )
                )
        return cls(passages, location_boost=location_boost)

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float = 0.0,
        boost_ids: Sequence[str] = (),
    ) -> list[tuple[Passage, float]]:
        """Топ-`top_k` пассажей по BM25-скору, отсечённых по `min_score`.

        Пустой результат -- ВАЖНЫЙ случай, не ошибка: означает, что модель
        обязана честно сказать «не знаю» (правило грунтования в
        `config/system_prompt.txt`), а не то, что поиск не удался.
        `boost_ids` -- id пассажей, привязанных к текущей остановке
        (`Passage.location_ids` пересекает `stop_id`), их скор умножается
        на `location_boost`, заданный при создании ретривера.
        """
        if self._bm25 is None:
            return []
        query_tokens = _normalize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        boost = frozenset(boost_ids)
        ranked = [
            (
                entry.passage,
                float(score) * (self._location_boost if entry.passage.id in boost else 1.0),
            )
            for entry, score in zip(self._entries, scores, strict=True)
        ]
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return [(passage, score) for passage, score in ranked[:top_k] if score >= min_score]
