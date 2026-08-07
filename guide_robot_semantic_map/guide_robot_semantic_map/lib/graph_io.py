"""Загрузка и валидация config/graph.geojson -- графа Route Server.

Формат -- GeoJSON FeatureCollection, который штатно понимает
nav2_route::GeoJsonGraphFileLoader (design.md ยง0.1): узлы -- Point-фичи,
рёбра -- LineString-фичи, id узлов и рёбер целые в диапазоне uint16
(design.md §0.5 -- ComputeRoute.start_id/goal_id используют этот тип).
Модуль не знает про rclpy и не знает про locations.yaml -- он отвечает
только за то, что граф сам по себе структурно корректен.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Graph", "GraphEdge", "GraphError", "GraphNode", "load_graph", "parse_graph"]

_MAX_NODE_ID = 65535  # uint16 -- ComputeRoute.start_id / goal_id
_COORDINATES_MIN_LEN = 2  # [x, y] или [x, y, z]


class GraphError(ValueError):
    """Граф структурно некорректен или ссылается сам на себя неверно."""


@dataclass(frozen=True)
class GraphNode:
    """Узел графа Route Server."""

    id: int
    x: float
    y: float
    frame: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class GraphEdge:
    """Ребро графа Route Server."""

    id: int
    start_id: int
    end_id: int
    cost: float | None
    overridable: bool | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Graph:
    """Разобранный и провалидированный граф."""

    nodes: dict[int, GraphNode]
    edges: dict[int, GraphEdge]


def load_graph(path: str | Path) -> Graph:
    """Прочитать и провалидировать graph.geojson с диска."""
    location = Path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphError(f"{location}: не удалось прочитать файл: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphError(f"{location}: невалидный JSON: {exc}") from exc
    return parse_graph(document, source=str(location))


def parse_graph(document: dict[str, Any], *, source: str = "<memory>") -> Graph:
    """Разобрать и провалидировать уже распарсенный GeoJSON-документ."""
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise GraphError(f"{source}: корневой объект должен быть FeatureCollection")

    nodes: dict[int, GraphNode] = {}
    edges: dict[int, GraphEdge] = {}

    for index, feature in enumerate(document.get("features", [])):
        geometry = feature.get("geometry") or {}
        geometry_type = geometry.get("type")
        if geometry_type == "Point":
            node = _parse_node(feature, index, source)
            if node.id in nodes:
                raise GraphError(f"{source}: дублирующийся id узла {node.id}")
            nodes[node.id] = node
        elif geometry_type == "LineString":
            edge = _parse_edge(feature, index, source)
            if edge.id in edges:
                raise GraphError(f"{source}: дублирующийся id ребра {edge.id}")
            edges[edge.id] = edge
        else:
            raise GraphError(
                f"{source}: features[{index}] имеет неизвестный geometry.type={geometry_type!r}"
            )

    if not nodes:
        raise GraphError(f"{source}: граф не содержит ни одного узла")

    for edge in edges.values():
        if edge.start_id not in nodes:
            raise GraphError(
                f"{source}: ребро {edge.id} ссылается на несуществующий startid={edge.start_id}"
            )
        if edge.end_id not in nodes:
            raise GraphError(
                f"{source}: ребро {edge.id} ссылается на несуществующий endid={edge.end_id}"
            )

    return Graph(nodes=nodes, edges=edges)


def _parse_feature_id(properties: dict[str, Any], index: int, source: str, kind: str) -> int:
    if "id" not in properties:
        raise GraphError(f"{source}: {kind}[{index}] без properties.id")
    raw_id = properties["id"]
    # bool -- подкласс int в Python, isinstance(True, int) is True.
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise GraphError(f"{source}: {kind}[{index}].id должен быть целым числом, есть {raw_id!r}")
    if not (0 <= raw_id <= _MAX_NODE_ID):
        raise GraphError(
            f"{source}: {kind}[{index}].id={raw_id} вне диапазона uint16 [0, {_MAX_NODE_ID}]"
        )
    return raw_id


def _parse_metadata(
    properties: dict[str, Any], feature_id: int, source: str, kind: str
) -> dict[str, Any]:
    metadata = properties.get("metadata", {})
    if not isinstance(metadata, dict):
        raise GraphError(f"{source}: {kind} {feature_id}.metadata должен быть объектом")
    return metadata


def _parse_node(feature: dict[str, Any], index: int, source: str) -> GraphNode:
    properties = feature.get("properties") or {}
    node_id = _parse_feature_id(properties, index, source, "узел")
    coordinates = (feature.get("geometry") or {}).get("coordinates")
    if not (isinstance(coordinates, list) and len(coordinates) >= _COORDINATES_MIN_LEN):
        raise GraphError(f"{source}: узел {node_id} без корректных coordinates")
    try:
        x, y = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError) as exc:
        raise GraphError(f"{source}: узел {node_id} имеет нечисловые coordinates") from exc
    frame = properties.get("frame")
    if frame is not None and not isinstance(frame, str):
        raise GraphError(f"{source}: узел {node_id}.frame должен быть строкой")
    return GraphNode(
        id=node_id,
        x=x,
        y=y,
        frame=frame,
        metadata=_parse_metadata(properties, node_id, source, "узел"),
    )


def _parse_edge(feature: dict[str, Any], index: int, source: str) -> GraphEdge:
    properties = feature.get("properties") or {}
    edge_id = _parse_feature_id(properties, index, source, "ребро")
    if "startid" not in properties or "endid" not in properties:
        raise GraphError(f"{source}: ребро {edge_id} без startid/endid")
    try:
        start_id = int(properties["startid"])
        end_id = int(properties["endid"])
    except (TypeError, ValueError) as exc:
        raise GraphError(f"{source}: ребро {edge_id} имеет нечисловые startid/endid") from exc
    cost = properties.get("cost")
    if cost is not None:
        cost = float(cost)
    overridable = properties.get("overridable")
    if overridable is not None and not isinstance(overridable, bool):
        raise GraphError(f"{source}: ребро {edge_id}.overridable должен быть булевым")
    return GraphEdge(
        id=edge_id,
        start_id=start_id,
        end_id=end_id,
        cost=cost,
        overridable=overridable,
        metadata=_parse_metadata(properties, edge_id, source, "ребро"),
    )
