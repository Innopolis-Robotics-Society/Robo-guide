"""`kb.verbatim.max_shingle_overlap()` -- чистая логика, без ROS (DIALOG_REWORK_PLAN.md §3.2)."""

from __future__ import annotations

from guide_robot_llm.kb.verbatim import max_shingle_overlap


def test_exact_long_quote_returns_full_word_count() -> None:
    passage = "Город основан указом президента в две тысячи двенадцатом году на берегу реки."
    answer = "Как рассказано в источнике: город основан указом президента в две тысячи."

    overlap = max_shingle_overlap(answer, [passage])
    assert overlap == 7  # "город основан указом президента в две тысячи"


def test_paraphrase_has_low_overlap() -> None:
    passage = "Город основан указом президента в две тысячи двенадцатом году."
    answer = "Университет появился благодаря решению главы государства недавно."

    assert max_shingle_overlap(answer, [passage]) <= 2


def test_no_overlap_returns_zero() -> None:
    assert max_shingle_overlap("совсем другой текст", ["ничего общего тут нет"]) == 0


def test_empty_answer_returns_zero() -> None:
    assert max_shingle_overlap("", ["что угодно тут написано"]) == 0


def test_best_overlap_picked_across_multiple_passages() -> None:
    answer = "город основан указом президента в две тысячи двенадцатом году"
    passages = ["ничего общего", "город основан указом президента в две тысячи двенадцатом году"]

    assert max_shingle_overlap(answer, passages) == 9


def test_overlap_is_case_and_yo_insensitive() -> None:
    passage = "Ещё немного про историю основания города."
    answer = "ЕЩЕ немного ПРО историю основания города"

    assert max_shingle_overlap(answer, [passage]) == 6
