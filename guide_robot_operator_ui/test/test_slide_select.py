"""Юниты lib/slide_select.py -- без rclpy, без ROS-графа, без браузера (design D3)."""

from __future__ import annotations

from dataclasses import dataclass

from guide_robot_operator_ui.lib.slide_select import resolve_chunk_id, select_media


@dataclass
class _Item:
    chunk_id: str
    id: str = "m"


# -- resolve_chunk_id ---------------------------------------------------------


def test_resolve_chunk_id_in_range() -> None:
    assert resolve_chunk_id(["c1", "c2", "c3"], 1) == "c2"


def test_resolve_chunk_id_at_start() -> None:
    assert resolve_chunk_id(["c1", "c2"], 0) == "c1"


def test_resolve_chunk_id_out_of_range_is_none_not_error() -> None:
    """design D1: прогресс не монотонен, откат/скачок chunk_index -- не исключение."""
    assert resolve_chunk_id(["c1", "c2"], 5) is None
    assert resolve_chunk_id(["c1", "c2"], -1) is None


def test_resolve_chunk_id_empty_manifest() -> None:
    assert resolve_chunk_id([], 0) is None


# -- select_media --------------------------------------------------------------


def test_select_media_matches_current_chunk() -> None:
    items = [_Item(chunk_id="c1", id="m1"), _Item(chunk_id="c2", id="m2")]
    assert [i.id for i in select_media(items, "c2")] == ["m2"]


def test_select_media_multiple_items_same_chunk_preserve_order() -> None:
    items = [_Item(chunk_id="c1", id="a"), _Item(chunk_id="c1", id="b")]
    assert [i.id for i in select_media(items, "c1")] == ["a", "b"]


def test_select_media_falls_back_to_exhibit_level_when_no_chunk_match() -> None:
    items = [_Item(chunk_id="", id="title-slide"), _Item(chunk_id="c1", id="m1")]
    assert [i.id for i in select_media(items, "c9")] == ["title-slide"]


def test_select_media_falls_back_when_chunk_id_is_none() -> None:
    """chunk_index вне диапазона -> resolve_chunk_id вернул None -> уровень экспоната."""
    items = [_Item(chunk_id="", id="title-slide"), _Item(chunk_id="c1", id="m1")]
    assert [i.id for i in select_media(items, None)] == ["title-slide"]


def test_select_media_empty_manifest_returns_empty_list() -> None:
    assert select_media([], "c1") == []


def test_select_media_no_exhibit_level_fallback_available_returns_empty() -> None:
    items = [_Item(chunk_id="c1", id="m1")]
    assert select_media(items, "c9") == []
