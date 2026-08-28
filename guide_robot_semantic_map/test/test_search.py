"""Юниты на `lib/search.py::ContentIndex` (стемминг, буст, фильтр kind, пустой запрос).

Перенесённые кейсы из guide_robot_llm/test/test_kb_retriever.py --
CLAUDE_CODE_TASK_stage1_knowledge.md §4.4.
"""

from __future__ import annotations

from guide_robot_semantic_map.lib.content_io import Chunk, ExhibitContent
from guide_robot_semantic_map.lib.search import ContentIndex


def _item(
    exhibit_id: str,
    title: str,
    chunks: list[Chunk],
    *,
    kind: str = "exhibit",
    location_ids: list[str] | None = None,
) -> ExhibitContent:
    return ExhibitContent(
        exhibit_id=exhibit_id,
        language="ru",
        version="v1",
        title=title,
        chunks=chunks,
        reviewed_by=None,
        reviewed_at=None,
        kind=kind,
        location_ids=location_ids if location_ids is not None else [exhibit_id],
    )


def test_empty_corpus_returns_empty() -> None:
    index = ContentIndex([])
    assert index.search("лидар") == []


def test_empty_query_returns_empty() -> None:
    index = ContentIndex(
        [_item("livox_mid70", "Лидар", [Chunk(id="c1", level="short", text="Это лидар Livox.")])]
    )
    assert index.search("   ") == []


def test_query_with_no_shared_terms_returns_empty() -> None:
    """stage2 A3: BM25-ноль (нет общих термов) отбрасывается, не добивает top_k мусором."""
    index = ContentIndex(
        [_item("livox_mid70", "Лидар", [Chunk(id="c1", level="short", text="Это лидар Livox.")])]
    )
    assert index.search("промобот") == []


def test_finds_exact_word() -> None:
    index = ContentIndex(
        [_item("livox_mid70", "Лидар", [Chunk(id="c1", level="short", text="Это лидар Livox.")])]
    )
    hits = index.search("лидар")
    assert len(hits) == 1
    assert hits[0].content_id == "livox_mid70"
    assert hits[0].chunk_id == "c1"


def test_stemming_matches_inflected_query() -> None:
    index = ContentIndex(
        [_item("livox_mid70", "Лидар", [Chunk(id="c1", level="short", text="Это лидар Livox.")])]
    )
    hits = index.search("расскажи про лидара")
    assert len(hits) == 1
    assert hits[0].content_id == "livox_mid70"


def test_location_boost_reorders_results() -> None:
    # Пять пунктов (не два) и разная длина текста -- избегаем вырожденного
    # случая rank_bm25, где IDF термина, встреченного ровно в половине
    # корпуса, точно равен нулю (n == N/2 => idf == 0 => score == 0 для
    # всех документов, буст в этом случае нечего умножать).
    index = ContentIndex(
        [
            _item(
                "robo_guide",
                "Первый",
                [Chunk(id="c1", level="short", text="Экскурсовод весит двадцать килограммов.")],
                location_ids=["robo_guide"],
            ),
            _item(
                "nav2_course",
                "Второй",
                [
                    Chunk(
                        id="c1",
                        level="short",
                        text=(
                            "Экскурсовод рассказывает про стек навигации Nav2 и "
                            "планирование маршрута с объездом препятствий."
                        ),
                    )
                ],
                location_ids=["nav2_course"],
            ),
            _item(
                "filler_a",
                "Третий",
                [Chunk(id="c1", level="short", text="Здесь стоит манипулятор и рисует картины.")],
                location_ids=["filler_a"],
            ),
            _item(
                "filler_b",
                "Четвёртый",
                [Chunk(id="c1", level="short", text="Тут показывают разметку данных.")],
                location_ids=["filler_b"],
            ),
            _item(
                "filler_c",
                "Пятый",
                [Chunk(id="c1", level="short", text="Лидар сканирует пространство лазером.")],
                location_ids=["filler_c"],
            ),
        ]
    )
    unboosted = index.search("экскурсовод")
    assert unboosted[0].content_id == "robo_guide"

    boosted = index.search("экскурсовод", boost_location_ids=["nav2_course"])
    assert boosted[0].content_id == "nav2_course"


def test_kind_filter_excludes_other_kinds() -> None:
    index = ContentIndex(
        [
            _item(
                "innopolis_city",
                "Иннополис",
                [Chunk(id="c1", level="short", text="Иннополис — город в Татарстане.")],
                kind="city",
                location_ids=[],
            ),
            _item(
                "robo_guide",
                "Экскурсовод",
                [Chunk(id="c1", level="short", text="Город показывают на экскурсии.")],
                kind="exhibit",
            ),
        ]
    )
    hits = index.search("город", kinds=["city"])
    assert [hit.content_id for hit in hits] == ["innopolis_city"]


def test_top_k_limits_results() -> None:
    index = ContentIndex(
        [
            _item(
                f"exhibit_{i}",
                f"Экспонат {i}",
                [Chunk(id="c1", level="short", text="Экспонат про робота.")],
            )
            for i in range(5)
        ]
    )
    hits = index.search("робот", top_k=2)
    assert len(hits) == 2
