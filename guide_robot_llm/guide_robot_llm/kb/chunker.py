"""Офлайн-разбор `.md` текста экскурсовода на пассажи (DIALOG_REWORK_PLAN.md §3.2).

Чистая логика, без rclpy и без BM25 -- `scripts/build_kb.py` зовёт это офлайн,
результат (`config/kb.jsonl`) коммитится в репозиторий, рантайм (`retriever.py`)
только читает уже собранный jsonl, не пересобирает корпус на лету.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Passage", "chunk_markdown"]

_DEFAULT_MAX_CHARS_PER_PASSAGE = 600
_DEFAULT_MIN_SECTION_CHARS = 120

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_LOCATION_IDS_RE = re.compile(r"^location_ids:\s*(.+)$", re.IGNORECASE)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Passage:
    """Один пассаж корпуса: id + цепочка заголовков + текст + опциональная привязка к локациям."""

    id: str  # "kb_0007"
    heading: str  # цепочка заголовков "Иннополис > История > Основание"
    text: str
    location_ids: tuple[str, ...] = ()


def chunk_markdown(
    markdown: str,
    *,
    max_chars_per_passage: int = _DEFAULT_MAX_CHARS_PER_PASSAGE,
    min_section_chars: int = _DEFAULT_MIN_SECTION_CHARS,
) -> list[Passage]:
    """Разбить `.md` на `Passage` по заголовкам `##`/`###`.

    Правила (DIALOG_REWORK_PLAN.md §3.2): резать по заголовкам; секция
    длиннее `max_chars_per_passage` режется по абзацам с перекрытием в один
    абзац; секция короче `min_section_chars` приклеивается к следующей
    (к предыдущей -- если это хвост документа без следующей секции).
    Заголовок `#` верхнего уровня -- заголовок документа, участвует в
    цепочке заголовков (`heading`), но сам по себе секцию не образует.
    """
    sections = _split_sections(markdown)
    entries = [(chain, *_split_location_ids(body)) for chain, body in sections]
    merged = _merge_short_sections(entries, min_section_chars)

    passages: list[Passage] = []
    counter = 0
    for chain, location_ids, body in merged:
        heading = " > ".join(chain)
        for chunk_text in _split_long_body(body, max_chars_per_passage):
            passages.append(
                Passage(
                    id=f"kb_{counter:04d}",
                    heading=heading,
                    text=chunk_text,
                    location_ids=location_ids,
                )
            )
            counter += 1
    return passages


def _split_sections(markdown: str) -> list[tuple[tuple[str, ...], str]]:
    """Разбить документ на (цепочка_заголовков, тело) по границам `#`/`##`/`###`."""
    matches = list(_HEADING_RE.finditer(markdown))
    sections: list[tuple[tuple[str, ...], str]] = []
    doc_title: str | None = None
    heading2: str | None = None

    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()

        if level == 1:
            doc_title = title
            continue
        if level == 2:
            heading2 = title
            chain_parts = [doc_title, title]
        else:  # level == 3
            chain_parts = [doc_title, heading2, title]
        chain = tuple(part for part in chain_parts if part)

        if body:
            sections.append((chain, body))
    return sections


def _split_location_ids(body: str) -> tuple[tuple[str, ...], str]:
    """Снять первую строку `location_ids: a, b` из тела секции, если она там есть."""
    first_line, _, rest = body.partition("\n")
    match = _LOCATION_IDS_RE.match(first_line.strip())
    if not match:
        return (), body
    ids = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    return ids, rest.strip()


def _merge_short_sections(
    entries: list[tuple[tuple[str, ...], tuple[str, ...], str]], min_section_chars: int
) -> list[tuple[tuple[str, ...], tuple[str, ...], str]]:
    """Приклеить секции короче `min_section_chars` к следующей (к предыдущей -- для хвоста)."""
    merged: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
    pending: tuple[tuple[str, ...], tuple[str, ...], str] | None = None

    for chain, location_ids, body in entries:
        if pending is not None:
            body = _join(pending[2], body)
            location_ids = _dedup(pending[1] + location_ids)
            pending = None
        if len(body) < min_section_chars:
            pending = (chain, location_ids, body)
            continue
        merged.append((chain, location_ids, body))

    if pending is not None:
        if merged:
            prev_chain, prev_ids, prev_body = merged[-1]
            merged[-1] = (
                prev_chain,
                _dedup(prev_ids + pending[1]),
                _join(prev_body, pending[2]),
            )
        else:
            merged.append(pending)
    return merged


def _join(first: str, second: str) -> str:
    """Склеить два тела через пустую строку, не оставляя ведущий/задвоенный разделитель."""
    return "\n\n".join(part for part in (first, second) if part)


def _dedup(ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ids))


def _split_long_body(body: str, max_chars: int) -> list[str]:
    """Разрезать `body` по абзацам с перекрытием в один абзац, если длиннее `max_chars`."""
    if len(body) <= max_chars:
        return [body]
    paragraphs = [p for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]
    if len(paragraphs) <= 1:
        return [body]  # один абзац длиннее лимита -- не резать посреди абзаца

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        added_len = len(paragraph) + 2
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [current[-1], paragraph]  # перекрытие в один абзац
            current_len = len(current[0]) + added_len
        else:
            current.append(paragraph)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks
