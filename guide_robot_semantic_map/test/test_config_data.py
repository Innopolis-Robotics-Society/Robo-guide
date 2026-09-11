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


def test_lab_demo_tour_stops_have_content() -> None:
    # "intro" (остановка entrance) больше не пробел -- content/intro.ru.yaml
    # добавлен вместе с миграцией kb_source/tour.md
    # (CLAUDE_CODE_TASK_stage1_knowledge.md §2.1).
    content, _ = load_content_dir(_CONTENT_DIR)
    tours = load_tours(_CONFIG_DIR / "tours.yaml")
    stop_exhibit_ids = {stop.exhibit_id for stop in tours.tours["lab_demo"].stops}
    covered = {exhibit_id for exhibit_id, _language in content}
    missing = stop_exhibit_ids - covered
    assert missing == set()


# -- Innopark (expo_one, реальная площадка) --------------------------------


def test_graph_innopark_geojson_is_valid() -> None:
    graph = load_graph(_CONFIG_DIR / "graph_innopark.geojson")
    assert len(graph.nodes) == 5


def test_locations_innopark_yaml_is_valid_and_linked_to_graph() -> None:
    graph = load_graph(_CONFIG_DIR / "graph_innopark.geojson")
    locations = load_locations(_CONFIG_DIR / "locations_innopark.yaml")
    validate_locations(locations)
    validate_graph_links(locations, set(graph.nodes))
    assert "expo_meeting" in locations.locations
    assert "right_wing" in locations.locations
    assert "left_wing" in locations.locations


def test_tours_innopark_yaml_references_valid_locations() -> None:
    locations = load_locations(_CONFIG_DIR / "locations_innopark.yaml")
    tours = load_tours(_CONFIG_DIR / "tours_innopark.yaml")
    validate_tours(tours, locations)
    assert "expo_one" in tours.tours
    assert len(tours.tours["expo_one"].stops) == 5


def test_expo_one_tour_has_known_content_gaps() -> None:
    # right_wing/left_wing -- новые остановки площадки, контент под них ещё
    # не написан (см. TODO в locations_innopark.yaml/tours_innopark.yaml).
    # Тест зафиксирован явно: если появится content/right_wing.ru.yaml или
    # content/left_wing.ru.yaml, множество missing должно уменьшиться --
    # тест начнёт падать, и это будет сигналом обновить его, а не тихий
    # пробел в турах.
    content, _ = load_content_dir(_CONTENT_DIR)
    tours = load_tours(_CONFIG_DIR / "tours_innopark.yaml")
    stop_exhibit_ids = {stop.exhibit_id for stop in tours.tours["expo_one"].stops}
    covered = {exhibit_id for exhibit_id, _language in content}
    missing = stop_exhibit_ids - covered
    assert missing == {"right_wing", "left_wing"}
