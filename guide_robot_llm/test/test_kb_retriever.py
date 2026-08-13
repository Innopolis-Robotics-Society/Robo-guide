"""`kb.retriever.BM25Retriever` -- чистая логика, без ROS (DIALOG_REWORK_PLAN.md §3.2)."""

from __future__ import annotations

import json

from guide_robot_llm.kb.chunker import Passage
from guide_robot_llm.kb.retriever import BM25Retriever

_LAB = Passage(
    id="kb_0000",
    heading="Иннополис > Лаборатория робототехники",
    text="В лаборатории роботов стоят демонстрационные макеты и манипуляторы.",
)
_CAFE = Passage(
    id="kb_0001",
    heading="Иннополис > Кафе",
    text="В кафе можно перекусить между остановками тура по кампусу.",
)
_HISTORY = Passage(
    id="kb_0002",
    heading="Иннополис > История",
    text="Город основан указом президента и назван в честь инновационного профиля.",
)


def test_stemming_finds_inflected_query_form() -> None:
    retriever = BM25Retriever([_LAB, _CAFE, _HISTORY])
    results = retriever.search("расскажи про экскурсии по лаборатории", top_k=3)

    assert results
    assert results[0][0].id == _LAB.id


def test_min_score_filters_out_irrelevant_query() -> None:
    retriever = BM25Retriever([_LAB, _CAFE, _HISTORY])
    results = retriever.search("совершенно не связанный запрос про погоду", min_score=1.5)

    assert results == []


def test_boost_ids_reorders_results() -> None:
    retriever = BM25Retriever([_LAB, _CAFE, _HISTORY])
    plain = retriever.search("расскажи про кампус", top_k=3)
    boosted = retriever.search("расскажи про кампус", top_k=3, boost_ids=[_CAFE.id])

    assert boosted[0][0].id == _CAFE.id
    assert plain != boosted


def test_from_jsonl_roundtrip(tmp_path) -> None:
    # Минимум два пассажа -- с единственным документом в корпусе BM25 IDF
    # вырождается в отрицательный (терм есть в 100% документов), это
    # свойство метода, не то, что здесь проверяется (парсинг jsonl).
    path = tmp_path / "kb.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for passage, location_ids in ((_LAB, ["lab_demo"]), (_CAFE, [])):
            handle.write(
                json.dumps(
                    {
                        "id": passage.id,
                        "heading": passage.heading,
                        "text": passage.text,
                        "location_ids": location_ids,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    retriever = BM25Retriever.from_jsonl(str(path))
    results = retriever.search("роботы в лаборатории")

    assert results
    assert results[0][0].id == _LAB.id
    assert results[0][0].location_ids == ("lab_demo",)


def test_empty_corpus_returns_empty_list() -> None:
    retriever = BM25Retriever([])
    assert retriever.search("что угодно") == []


def test_query_of_only_stopwords_returns_empty_list() -> None:
    retriever = BM25Retriever([_LAB, _CAFE])
    assert retriever.search("и в на") == []
