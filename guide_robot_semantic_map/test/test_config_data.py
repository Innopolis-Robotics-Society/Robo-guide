"""Интеграционная сверка: реальные config/*.yaml и content/*.yaml валидны разом.

Не юниты на отдельный модуль -- проверка того, что данные лаборатории
(locations.yaml, tours.yaml, graph.geojson, content/*.yaml) реально
проходят через весь стек валидаторов lib/, а не только через фикстуры.
"""

from __future__ import annotations

from pathlib import Path

from guide_robot_semantic_map.lib.content_io import load_content_dir
from guide_robot_semantic_map.lib.graph_io import load_graph
from guide_robot_semantic_map.lib.locations_io import (
    load_locations,
    load_tours,
    validate_graph_links,
    validate_locations,
    validate_tours,
)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PACKAGE_ROOT / "config"
_CONTENT_DIR = _PACKAGE_ROOT / "content"


def test_graph_geojson_is_valid() -> None:
    graph = load_graph(_CONFIG_DIR / "graph.geojson")
    assert len(graph.nodes) == 6


def test_locations_yaml_is_valid_and_linked_to_graph() -> None:
    graph = load_graph(_CONFIG_DIR / "graph.geojson")
    locations = load_locations(_CONFIG_DIR / "locations.yaml")
    validate_locations(locations)
    validate_graph_links(locations, set(graph.nodes))
    assert "entrance" in locations.locations
    assert "livox_mid70" in locations.locations


def test_tours_yaml_references_valid_locations() -> None:
    locations = load_locations(_CONFIG_DIR / "locations.yaml")
    tours = load_tours(_CONFIG_DIR / "tours.yaml")
    validate_tours(tours, locations)
    assert "lab_demo" in tours.tours
    assert len(tours.tours["lab_demo"].stops) == 6


def test_content_dir_loads_without_errors() -> None:
    content, warnings = load_content_dir(_CONTENT_DIR)
    assert ("nav2_course", "ru") in content
    assert ("robo_guide", "ru") in content
    assert ("promobot_m13_artist", "ru") in content
    assert ("sam3_autolabeling", "ru") in content
    assert ("livox_mid70", "ru") in content
    assert warnings == []


def test_lab_demo_tour_stops_missing_content_are_known_gaps() -> None:
    # "intro" (остановка entrance) и claude_code_ros2_kit -- реальные
    # экскурсоводческие тексты, которых пока нет и которые этот модуль
    # не должен придумывать (design.md §1.3: content_server не порождает
    # текст). Тест документирует пробел, а не проверяет его отсутствие.
    content, _ = load_content_dir(_CONTENT_DIR)
    tours = load_tours(_CONFIG_DIR / "tours.yaml")
    stop_exhibit_ids = {stop.exhibit_id for stop in tours.tours["lab_demo"].stops}
    covered = {exhibit_id for exhibit_id, _language in content}
    missing = stop_exhibit_ids - covered
    assert missing == {"intro"}
