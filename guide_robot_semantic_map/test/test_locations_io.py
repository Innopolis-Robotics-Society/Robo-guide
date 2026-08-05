"""Юниты на парсинг и валидацию locations.yaml / tours.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from guide_robot_semantic_map.lib.locations_io import (
    Location,
    LocationsError,
    Pose2D,
    filter_near,
    is_visible,
    load_locations,
    load_tours,
    parse_locations,
    parse_tours,
    validate_graph_links,
    validate_locations,
    validate_tours,
)


def _location(location_id: str, graph_node: int = 1, **overrides: object) -> dict:
    base = {
        "id": location_id,
        "graph_node": graph_node,
        "pose": {"x": 1.0, "y": 2.0, "yaw": 0.0},
        "zone": "hall_a",
        "category": "waypoint",
        "is_public": True,
        "exhibit_id": None,
        "aliases": {"ru": [location_id], "en": [location_id]},
    }
    base.update(overrides)
    return base


def _locations_doc(*locations: dict) -> dict:
    return {"version": 1, "frame_id": "map", "locations": list(locations)}


def _tours_doc(*tours: dict) -> dict:
    return {"version": 1, "tours": list(tours)}


def _tour(tour_id: str, *stops: dict, default: bool = False) -> dict:
    return {
        "id": tour_id,
        "name": {"ru": tour_id, "en": tour_id},
        "default": default,
        "stops": list(stops),
    }


def _stop(location_id: str, exhibit_id: str = "x", dwell_s: int = 30, mode: str = "short") -> dict:
    return {"location_id": location_id, "exhibit_id": exhibit_id, "dwell_s": dwell_s, "mode": mode}


# -- locations.yaml -----------------------------------------------------------


def test_parses_valid_locations() -> None:
    doc = _locations_doc(
        _location("entrance", graph_node=1, category="waypoint"),
        _location("kandinsky", graph_node=4, category="exhibit", exhibit_id="kandinsky_viii"),
    )
    parsed = parse_locations(doc)
    assert set(parsed.locations) == {"entrance", "kandinsky"}
    assert parsed.locations["kandinsky"].exhibit_id == "kandinsky_viii"
    assert parsed.locations["entrance"].pose.x == pytest.approx(1.0)


def test_rejects_missing_graph_node() -> None:
    doc = _locations_doc(_location("entrance"))
    del doc["locations"][0]["graph_node"]
    with pytest.raises(LocationsError, match="graph_node обязателен"):
        parse_locations(doc)


def test_rejects_null_graph_node() -> None:
    doc = _locations_doc(_location("entrance", graph_node=None))
    with pytest.raises(LocationsError, match="graph_node обязателен"):
        parse_locations(doc)


def test_rejects_duplicate_location_id() -> None:
    doc = _locations_doc(_location("entrance"), _location("entrance"))
    with pytest.raises(LocationsError, match="дублирующийся id локации"):
        parse_locations(doc)


def test_rejects_unknown_category() -> None:
    doc = _locations_doc(_location("entrance", category="lobby"))
    with pytest.raises(LocationsError, match="category"):
        parse_locations(doc)


def test_rejects_missing_pose_field() -> None:
    doc = _locations_doc(_location("entrance", pose={"x": 1.0, "y": 2.0}))
    with pytest.raises(LocationsError, match="pose не хватает поля"):
        parse_locations(doc)


def test_rejects_non_bool_is_public() -> None:
    doc = _locations_doc(_location("entrance", is_public="yes"))
    with pytest.raises(LocationsError, match="is_public"):
        parse_locations(doc)


def test_rejects_empty_locations_list() -> None:
    with pytest.raises(LocationsError, match="непустым списком"):
        parse_locations({"version": 1, "frame_id": "map", "locations": []})


def test_exhibit_id_optional() -> None:
    doc = _locations_doc(_location("entrance", exhibit_id=None))
    parsed = parse_locations(doc)
    assert parsed.locations["entrance"].exhibit_id is None


# -- validate_locations: дубли алиасов -----------------------------------------


def test_duplicate_alias_same_language_rejected() -> None:
    doc = _locations_doc(
        _location("a", aliases={"ru": ["вход"]}),
        _location("b", aliases={"ru": ["Вход"]}),
    )
    parsed = parse_locations(doc)
    with pytest.raises(LocationsError, match="нормализуется одинаково"):
        validate_locations(parsed)


def test_same_alias_different_language_allowed() -> None:
    doc = _locations_doc(
        _location("a", aliases={"ru": ["вход"]}),
        _location("b", aliases={"en": ["вход"]}),
    )
    parsed = parse_locations(doc)
    validate_locations(parsed)  # не должно бросать


def test_alias_reused_within_same_location_allowed() -> None:
    doc = _locations_doc(_location("a", aliases={"ru": ["вход", "Вход!"]}))
    parsed = parse_locations(doc)
    validate_locations(parsed)  # оба алиаса нормализуются в "вход" одной локации


def test_alias_normalizing_to_empty_rejected() -> None:
    doc = _locations_doc(_location("a", aliases={"ru": ["   "]}))
    parsed = parse_locations(doc)
    with pytest.raises(LocationsError, match="пустую строку"):
        validate_locations(parsed)


# -- validate_graph_links -------------------------------------------------------


def test_graph_link_valid() -> None:
    doc = _locations_doc(_location("entrance", graph_node=1))
    parsed = parse_locations(doc)
    validate_graph_links(parsed, graph_node_ids={1, 2, 3})  # не должно бросать


def test_graph_link_dangling_rejected() -> None:
    doc = _locations_doc(_location("entrance", graph_node=99))
    parsed = parse_locations(doc)
    with pytest.raises(LocationsError, match="graph_node=99"):
        validate_graph_links(parsed, graph_node_ids={1, 2, 3})


# -- tours.yaml -----------------------------------------------------------


def test_parses_valid_tour() -> None:
    doc = _tours_doc(_tour("highlights", _stop("entrance"), default=True))
    parsed = parse_tours(doc)
    assert set(parsed.tours) == {"highlights"}
    assert parsed.tours["highlights"].default is True
    assert parsed.tours["highlights"].stops[0].location_id == "entrance"


def test_rejects_duplicate_tour_id() -> None:
    doc = _tours_doc(_tour("t", _stop("a")), _tour("t", _stop("b")))
    with pytest.raises(LocationsError, match="дублирующийся id тура"):
        parse_tours(doc)


def test_rejects_invalid_mode() -> None:
    doc = _tours_doc(_tour("t", _stop("a", mode="medium")))
    with pytest.raises(LocationsError, match="mode"):
        parse_tours(doc)


def test_rejects_empty_stops() -> None:
    doc = _tours_doc(_tour("t"))
    with pytest.raises(LocationsError, match="stops должен быть непустым"):
        parse_tours(doc)


def test_validate_tours_dangling_location_rejected() -> None:
    locations_doc = _locations_doc(_location("entrance"))
    tours_doc = _tours_doc(_tour("t", _stop("nowhere")))
    locations = parse_locations(locations_doc)
    tours = parse_tours(tours_doc)
    with pytest.raises(LocationsError, match=r"не найден в locations\.yaml"):
        validate_tours(tours, locations)


def test_validate_tours_valid_reference() -> None:
    locations_doc = _locations_doc(_location("entrance"))
    tours_doc = _tours_doc(_tour("t", _stop("entrance")))
    locations = parse_locations(locations_doc)
    tours = parse_tours(tours_doc)
    validate_tours(tours, locations)  # не должно бросать


# -- load_* с диска -----------------------------------------------------------


def test_load_locations_round_trip(tmp_path: Path) -> None:
    doc = _locations_doc(_location("entrance"))
    path = tmp_path / "locations.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    parsed = load_locations(path)
    assert "entrance" in parsed.locations


def test_load_tours_round_trip(tmp_path: Path) -> None:
    doc = _tours_doc(_tour("t", _stop("entrance")))
    path = tmp_path / "tours.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    parsed = load_tours(path)
    assert "t" in parsed.tours


def test_load_locations_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LocationsError, match="не удалось прочитать"):
        load_locations(tmp_path / "missing.yaml")


def test_load_locations_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "locations.yaml"
    path.write_text("locations: [unclosed", encoding="utf-8")
    with pytest.raises(LocationsError, match="невалидный YAML"):
        load_locations(path)


# -- is_visible (design.md §1.1) ------------------------------------------------


def _loc(
    location_id: str,
    *,
    is_public: bool,
    category: str = "exhibit",
    x: float = 0.0,
    y: float = 0.0,
) -> Location:
    return Location(
        id=location_id,
        graph_node=1,
        pose=Pose2D(x=x, y=y, yaw=0.0),
        zone="hall_a",
        category=category,
        is_public=is_public,
        exhibit_id=None,
        aliases={},
    )


def test_public_location_always_visible() -> None:
    loc = _loc("a", is_public=True, category="exhibit")
    assert is_visible(loc, "") is True
    assert is_visible(loc, "service") is True


def test_hidden_location_invisible_by_default() -> None:
    loc = _loc("a", is_public=False, category="service")
    assert is_visible(loc, "") is False
    assert is_visible(loc, "exhibit") is False


def test_hidden_location_visible_for_matching_service_category_query() -> None:
    loc = _loc("a", is_public=False, category="service")
    assert is_visible(loc, "service") is True


def test_hidden_charging_visible_for_matching_charging_category_query() -> None:
    loc = _loc("a", is_public=False, category="charging")
    assert is_visible(loc, "charging") is True


def test_hidden_location_not_leaked_by_mismatched_category_query() -> None:
    # is_public=false, category=service, но запрос за charging -- не должен
    # случайно приоткрыть чужую служебную категорию.
    loc = _loc("a", is_public=False, category="service")
    assert is_visible(loc, "charging") is False


# -- filter_near (design.md §1.1) -----------------------------------------------


def test_filter_near_sorts_by_distance() -> None:
    far = _loc("far", is_public=True, x=10.0, y=0.0)
    near = _loc("near", is_public=True, x=1.0, y=0.0)
    mid = _loc("mid", is_public=True, x=5.0, y=0.0)
    result = filter_near([far, near, mid], robot_x=0.0, robot_y=0.0, radius_m=100.0)
    assert [loc.id for loc in result] == ["near", "mid", "far"]


def test_filter_near_cuts_off_radius() -> None:
    inside = _loc("inside", is_public=True, x=2.0, y=0.0)
    outside = _loc("outside", is_public=True, x=8.0, y=0.0)
    result = filter_near([inside, outside], robot_x=0.0, robot_y=0.0, radius_m=5.0)
    assert [loc.id for loc in result] == ["inside"]


def test_filter_near_empty_input() -> None:
    assert filter_near([], robot_x=0.0, robot_y=0.0, radius_m=5.0) == []


def test_filter_near_uses_euclidean_distance() -> None:
    # 3-4-5 треугольник: расстояние ровно 5, значит попадает в радиус 5.0.
    loc = _loc("a", is_public=True, x=3.0, y=4.0)
    result = filter_near([loc], robot_x=0.0, robot_y=0.0, radius_m=5.0)
    assert result == [loc]
