"""Юниты на парсинг и валидацию graph.geojson."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guide_robot_semantic_map.lib.graph_io import GraphError, load_graph, parse_graph


def _node(node_id: int, x: float, y: float, **properties: object) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [x, y]},
        "properties": {"id": node_id, **properties},
    }


def _edge(edge_id: int, start_id: int, end_id: int, **properties: object) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        "properties": {"id": edge_id, "startid": start_id, "endid": end_id, **properties},
    }


def _collection(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_parses_nodes_and_edges() -> None:
    document = _collection(
        _node(1, 0.0, 0.0, frame="map", metadata={"name": "entrance"}),
        _node(2, 3.5, -1.2),
        _edge(10, 1, 2, cost=1.5, overridable=True, metadata={"class": "corridor"}),
    )
    graph = parse_graph(document)
    assert set(graph.nodes) == {1, 2}
    assert graph.nodes[1].frame == "map"
    assert graph.nodes[1].metadata == {"name": "entrance"}
    assert graph.nodes[2].x == pytest.approx(3.5)
    assert graph.edges[10].start_id == 1
    assert graph.edges[10].end_id == 2
    assert graph.edges[10].cost == pytest.approx(1.5)
    assert graph.edges[10].overridable is True


def test_rejects_non_feature_collection() -> None:
    with pytest.raises(GraphError, match="FeatureCollection"):
        parse_graph({"type": "Feature"})


def test_rejects_duplicate_node_id() -> None:
    document = _collection(_node(1, 0.0, 0.0), _node(1, 1.0, 1.0))
    with pytest.raises(GraphError, match="дублирующийся id узла"):
        parse_graph(document)


def test_rejects_duplicate_edge_id() -> None:
    document = _collection(
        _node(1, 0.0, 0.0),
        _node(2, 1.0, 1.0),
        _node(3, 2.0, 2.0),
        _edge(10, 1, 2),
        _edge(10, 2, 3),
    )
    with pytest.raises(GraphError, match="дублирующийся id ребра"):
        parse_graph(document)


def test_rejects_dangling_start_id() -> None:
    document = _collection(_node(1, 0.0, 0.0), _edge(10, 99, 1))
    with pytest.raises(GraphError, match="startid=99"):
        parse_graph(document)


def test_rejects_dangling_end_id() -> None:
    document = _collection(_node(1, 0.0, 0.0), _edge(10, 1, 99))
    with pytest.raises(GraphError, match="endid=99"):
        parse_graph(document)


def test_rejects_empty_graph() -> None:
    with pytest.raises(GraphError, match="не содержит ни одного узла"):
        parse_graph(_collection())


def test_rejects_id_out_of_uint16_range() -> None:
    with pytest.raises(GraphError, match="вне диапазона uint16"):
        parse_graph(_collection(_node(70000, 0.0, 0.0)))


def test_rejects_negative_id() -> None:
    with pytest.raises(GraphError, match="вне диапазона uint16"):
        parse_graph(_collection(_node(-1, 0.0, 0.0)))


def test_rejects_non_integer_id() -> None:
    with pytest.raises(GraphError, match="целым числом"):
        parse_graph(_collection(_node("a", 0.0, 0.0)))


def test_rejects_bool_id() -> None:
    with pytest.raises(GraphError, match="целым числом"):
        parse_graph(_collection(_node(True, 0.0, 0.0)))


def test_rejects_unknown_geometry_type() -> None:
    document = _collection(
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": []},
            "properties": {"id": 1},
        }
    )
    with pytest.raises(GraphError, match=r"geometry\.type"):
        parse_graph(document)


def test_rejects_edge_without_start_or_end() -> None:
    document = _collection(
        _node(1, 0.0, 0.0),
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {"id": 10, "startid": 1},
        },
    )
    with pytest.raises(GraphError, match="startid/endid"):
        parse_graph(document)


def test_rejects_non_object_metadata() -> None:
    document = _collection(_node(1, 0.0, 0.0, metadata="not-a-dict"))
    with pytest.raises(GraphError, match="metadata должен быть объектом"):
        parse_graph(document)


def test_load_graph_reads_file(tmp_path: Path) -> None:
    document = _collection(_node(1, 0.0, 0.0), _node(2, 1.0, 1.0), _edge(10, 1, 2))
    path = tmp_path / "graph.geojson"
    path.write_text(json.dumps(document), encoding="utf-8")
    graph = load_graph(path)
    assert set(graph.nodes) == {1, 2}
    assert set(graph.edges) == {10}


def test_load_graph_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GraphError, match="не удалось прочитать"):
        load_graph(tmp_path / "missing.geojson")


def test_load_graph_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "graph.geojson"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GraphError, match="невалидный JSON"):
        load_graph(path)
