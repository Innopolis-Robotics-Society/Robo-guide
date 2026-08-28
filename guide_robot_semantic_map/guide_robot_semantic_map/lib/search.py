"""BM25-поиск по чанкам content/*.yaml (CLAUDE_CODE_TASK_stage1_knowledge.md §4.1).

Перенесено из guide_robot_llm/kb/retriever.py -- пакет ЛЛМ обращается к
семантической карте только через `~/search_content`, локального корпуса и
локального ретривера у него больше нет. Единица индекса -- один чанк
(`content_id, chunk_id`), не весь пассаж/файл: посетитель спрашивает про
конкретный факт, а не про весь текст экспоната сразу.

Нормализация запроса и корпуса -- тот же пайплайн NFC/lower/ё->е/схлопывание
пунктуации плюс русский Snowball-стеммер и список стоп-слов, что был в
retriever.py: без стемминга «лидара» в вопросе не находит «лидар» в тексте
чанка, а частотные служебные слова иначе доминируют в скоринге коротких
чанков.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import snowballstemmer
from rank_bm25 import BM25Okapi

from guide_robot_semantic_map.lib.content_io import ExhibitContent

__all__ = ["ContentIndex", "Hit"]

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_DEFAULT_LOCATION_BOOST = 1.5

# Частотные русские служебные слова (предлоги/союзы/частицы/местоимения) --
# без них короткие чанки ранжируются в основном по совпадению "и"/"в"/"на".
_STOPWORDS = frozenset(
    [
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она",
        "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее",
        "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда",
        "даже", "ну", "вдруг", "ли", "если", "или", "ни", "быть", "был", "него", "до", "вас",
        "нибудь", "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может",
        "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем", "была",
        "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет", "ж",
        "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь",
        "этом", "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда",
        "зачем", "всех", "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
        "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
        "много", "разве", "три", "эту", "моя", "впрочем", "свою", "этой", "перед", "иногда",
        "лучше", "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно", "всю",
        "между",
    ]
)

_stemmer = snowballstemmer.stemmer("russian")


def _normalize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    normalized = _WHITESPACE.sub(" ", stripped).strip()
    words = [word for word in normalized.split() if word not in _STOPWORDS]
    return _stemmer.stemWords(words) if words else []


@dataclass(frozen=True)
class Hit:
    """Один найденный чанк -- поля один в один с `guide_robot_msgs/msg/ContentHit`."""

    content_id: str
    kind: str
    title: str
    chunk_id: str
    text: str
    score: float
    version: str


@dataclass(frozen=True)
class _Entry:
    item: ExhibitContent
    chunk_id: str
    text: str
    tokens: list[str]


class ContentIndex:
    """BM25 поверх чанков одного языка (`content_server` строит один индекс на язык)."""

    def __init__(
        self, items: Iterable[ExhibitContent], *, location_boost: float = _DEFAULT_LOCATION_BOOST
    ) -> None:
        """Проиндексировать все чанки всех `items`. Пустой корпус -- валидный случай."""
        self._location_boost = location_boost
        self._entries: list[_Entry] = [
            _Entry(
                item=item,
                chunk_id=chunk.id,
                text=chunk.text,
                tokens=_normalize(f"{item.title} {chunk.text}"),
            )
            for item in items
            for chunk in item.chunks
        ]
        corpus_tokens = [entry.tokens for entry in self._entries]
        self._bm25 = BM25Okapi(corpus_tokens) if corpus_tokens else None

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        boost_location_ids: Sequence[str] = (),
        kinds: Sequence[str] = (),
    ) -> list[Hit]:
        """Топ-`top_k` чанков по BM25-скору, кроме тех, чей `kind` не входит в `kinds`.

        `kinds` -- фильтр (пусто = все kind допустимы), не буст.
        `boost_location_ids` -- id локаций текущей/следующей остановки: скор
        чанка умножается на `location_boost`, если `item.location_ids`
        пересекает `boost_location_ids`. Абсолютного `min_score` нет --
        живой баг retriever.py, порог на малом корпусе не настраивался.
        Пустой запрос -- пустой результат.
        """
        if self._bm25 is None:
            return []
        query_tokens = _normalize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        kind_filter = frozenset(kinds)
        boost = frozenset(boost_location_ids)

        ranked: list[tuple[_Entry, float]] = []
        for entry, raw_score in zip(self._entries, scores, strict=True):
            if kind_filter and entry.item.kind not in kind_filter:
                continue
            score = float(raw_score)
            if boost and set(entry.item.location_ids) & boost:
                score *= self._location_boost
            ranked.append((entry, score))

        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return [
            Hit(
                content_id=entry.item.exhibit_id,
                kind=entry.item.kind,
                title=entry.item.title,
                chunk_id=entry.chunk_id,
                text=entry.text,
                score=score,
                version=entry.item.version,
            )
            for entry, score in ranked[:top_k]
        ]
