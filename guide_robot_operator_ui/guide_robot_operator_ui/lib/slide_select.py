"""Выбор медиа для текущего (exhibit_id, chunk_index) -- без rclpy (design D3).

Правила (design D1, дословно): есть элементы с chunk_id текущего чанка ->
показать их; нет -> элементы уровня экспоната (chunk_id пуст); манифест
пуст или чанк не резолвится -> пустой список, вызывающий код обязан свести
это к титульной карточке, не к чёрному экрану.

Прогресс `/mission/state`'s chunk_index НЕ монотонен (design D1: барж-ин на
перегоне, SAFETY-приоритетный Say поверх нарратива) -- уменьшение или выход
chunk_index за диапазон chunk_ids это ОБЫЧНОЕ событие, не исключение;
`resolve_chunk_id` возвращает None молча, не бросает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

__all__ = ["resolve_chunk_id", "select_media"]


class _HasChunkId(Protocol):
    chunk_id: str


def resolve_chunk_id(chunk_ids: Sequence[str], chunk_index: int) -> str | None:
    """chunk_id по позиции в уже-full-mode списке (design D1) -- None вне диапазона."""
    if 0 <= chunk_index < len(chunk_ids):
        return chunk_ids[chunk_index]
    return None


def select_media(items: Sequence[_HasChunkId], chunk_id: str | None) -> list[Any]:
    """Список медиа для текущего чанка (design D1's правило выбора).

    Принимает что угодно с атрибутом `.chunk_id` -- реальные `MediaItem` из
    кэша узла или простые тестовые дублёры, поэтому тесты этого модуля не
    тянут `guide_robot_msgs`.
    """
    if chunk_id is not None:
        matching = [item for item in items if item.chunk_id == chunk_id]
        if matching:
            return matching
    return [item for item in items if item.chunk_id == ""]
