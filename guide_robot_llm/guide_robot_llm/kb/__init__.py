"""Корпус знаний экскурсовода: пассажи + BM25-поиск (DIALOG_REWORK_PLAN.md §3.2)."""

from __future__ import annotations

from guide_robot_llm.kb.chunker import Passage, chunk_markdown
from guide_robot_llm.kb.retriever import BM25Retriever
from guide_robot_llm.kb.verbatim import max_shingle_overlap

__all__ = ["BM25Retriever", "Passage", "chunk_markdown", "max_shingle_overlap"]
