"""Загрузка и валидация config/locations.yaml и config/tours.yaml (design.md §1.1, §2).

Разделение with graph_io.py умышленное (design.md §0.5): узел графа Route
Server знает только координаты и проходимость, а "встать лицом к
экспонату" -- это yaw, которого в графе нет. locations.yaml -- источник
истины по позе, зоне, категории и алиасам; связь с графом -- обязательное
целочисленное поле graph_node, проверяемое здесь против набора id узлов
из graph_io.Graph. Битая ссылка -- ошибка данных, а не runtime-деградация:
её ловит on_configure, а не посетитель музея.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from guide_robot_semantic_map.lib.text_norm import normalize

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Location",
    "LocationsError",
    "LocationsFile",
    "Pose2D",
    "Tour",
    "TourStop",
    "ToursFile",
    "filter_near",
    "is_visible",
    "load_locations",
    "load_tours",
    "parse_locations",
    "parse_tours",
    "validate_graph_links",
    "validate_locations",
    "validate_tours",
]

VALID_CATEGORIES: frozenset[str] = frozenset(["exhibit", "waypoint", "service", "charging"])
VALID_MODES: frozenset[str] = frozenset(["short", "full"])
SERVICE_CATEGORIES: frozenset[str] = frozenset(["charging", "service"])


class LocationsError(ValueError):
    """Ошибка формата или консистентности locations.yaml / tours.yaml."""


@dataclass(frozen=True)
class Pose2D:
    """Поза на плоскости карты. yaw -- куда развернётся робот, встав на точку."""

    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class Location:
    """Одна запись locations.yaml."""

    id: str
    graph_node: int
    pose: Pose2D
    zone: str
    category: str
    is_public: bool
    exhibit_id: str | None
    aliases: dict[str, list[str]]


@dataclass(frozen=True)
class LocationsFile:
    """Разобранный и провалидированный locations.yaml."""

    version: int
    frame_id: str
    locations: dict[str, Location]


@dataclass(frozen=True)
class TourStop:
    """Одна остановка тура."""

    location_id: str
    exhibit_id: str
    dwell_s: int
    mode: str


@dataclass(frozen=True)
class Tour:
    """Одна запись tours.yaml."""

    id: str
    name: dict[str, str]
    default: bool
    stops: list[TourStop]


@dataclass(frozen=True)
class ToursFile:
    """Разобранный и провалидированный tours.yaml."""

    version: int
    tours: dict[str, Tour]


# -- locations.yaml -----------------------------------------------------------


def load_locations(path: str | Path) -> LocationsFile:
    """Прочитать, разобрать и провалидировать locations.yaml с диска."""
    document = _read_yaml(path)
    parsed = parse_locations(document, source=str(path))
    validate_locations(parsed)
    return parsed


def parse_locations(document: dict[str, Any], *, source: str = "<memory>") -> LocationsFile:
    """Разобрать уже загруженный YAML-документ locations.yaml (без валидации)."""
    if not isinstance(document, dict):
        raise LocationsError(f"{source}: корневой объект должен быть отображением (mapping)")

    version = _require_int(document, "version", source)
    frame_id = _require_str(document, "frame_id", source)

    raw_locations = document.get("locations")
    if not isinstance(raw_locations, list) or not raw_locations:
        raise LocationsError(f"{source}: locations должен быть непустым списком")

    locations: dict[str, Location] = {}
    for index, raw in enumerate(raw_locations):
        location = _parse_location(raw, index, source)
        if location.id in locations:
            raise LocationsError(f"{source}: дублирующийся id локации {location.id!r}")
        locations[location.id] = location

    return LocationsFile(version=version, frame_id=frame_id, locations=locations)


def _parse_location(raw: dict[str, Any], index: int, source: str) -> Location:
    if not isinstance(raw, dict):
        raise LocationsError(f"{source}: locations[{index}] должен быть отображением")

    location_id = _require_str(raw, "id", source, where=f"locations[{index}]")
    where = f"{source}: локация {location_id!r}"

    graph_node = raw.get("graph_node")
    if isinstance(graph_node, bool) or not isinstance(graph_node, int):
        raise LocationsError(
            f"{where}.graph_node обязателен и должен быть целым числом узла графа"
        )

    pose = _parse_pose(raw.get("pose"), where)
    zone = _require_str(raw, "zone", source, where=where)
    category = _require_str(raw, "category", source, where=where)
    if category not in VALID_CATEGORIES:
        raise LocationsError(
            f"{where}.category={category!r} не входит в {sorted(VALID_CATEGORIES)}"
        )

    is_public = raw.get("is_public")
    if not isinstance(is_public, bool):
        raise LocationsError(f"{where}.is_public обязателен и должен быть булевым")

    exhibit_id = raw.get("exhibit_id")
    if exhibit_id is not None and not isinstance(exhibit_id, str):
        raise LocationsError(f"{where}.exhibit_id должен быть строкой или null")

    aliases = _parse_aliases(raw.get("aliases"), where)

    return Location(
        id=location_id,
        graph_node=graph_node,
        pose=pose,
        zone=zone,
        category=category,
        is_public=is_public,
        exhibit_id=exhibit_id,
        aliases=aliases,
    )


def _parse_pose(raw: Any, where: str) -> Pose2D:
    if not isinstance(raw, dict):
        raise LocationsError(f"{where}.pose обязателен и должен быть отображением {{x, y, yaw}}")
    try:
        return Pose2D(x=float(raw["x"]), y=float(raw["y"]), yaw=float(raw["yaw"]))
    except KeyError as exc:
        raise LocationsError(f"{where}.pose не хватает поля {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise LocationsError(f"{where}.pose содержит нечисловое значение: {exc}") from exc


def _parse_aliases(raw: Any, where: str) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LocationsError(f"{where}.aliases должен быть отображением language -> [строки]")
    aliases: dict[str, list[str]] = {}
    for language, values in raw.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise LocationsError(f"{where}.aliases[{language!r}] должен быть списком строк")
        aliases[language] = list(values)
    return aliases


def validate_locations(parsed: LocationsFile) -> None:
    """Проверить инварианты, не требующие графа: категории и уникальность алиасов.

    Дубль id уже отловлен при разборе (dict не даёт двух записей с одним
    ключом). Здесь -- инвариант design.md §1.1: два разных нормализованных
    алиаса в пределах одного языка не могут указывать на разные локации.
    """
    seen: dict[tuple[str, str], str] = {}
    for location in parsed.locations.values():
        for language, aliases in location.aliases.items():
            for alias in aliases:
                key = (language, normalize(alias))
                if key[1] == "":
                    raise LocationsError(
                        f"локация {location.id!r}: алиас {alias!r} ({language}) "
                        "нормализуется в пустую строку"
                    )
                owner = seen.get(key)
                if owner is not None and owner != location.id:
                    raise LocationsError(
                        f"алиас {alias!r} ({language}) нормализуется одинаково для "
                        f"{owner!r} и {location.id!r}"
                    )
                seen[key] = location.id


def validate_graph_links(parsed: LocationsFile, graph_node_ids: set[int]) -> None:
    """Проверить, что graph_node каждой локации существует в графе Route Server."""
    for location in parsed.locations.values():
        if location.graph_node not in graph_node_ids:
            raise LocationsError(
                f"локация {location.id!r}: graph_node={location.graph_node} "
                "отсутствует в graph.geojson"
            )


# -- tours.yaml -----------------------------------------------------------


def load_tours(path: str | Path) -> ToursFile:
    """Прочитать, разобрать и провалидировать tours.yaml с диска."""
    document = _read_yaml(path)
    return parse_tours(document, source=str(path))


def parse_tours(document: dict[str, Any], *, source: str = "<memory>") -> ToursFile:
    """Разобрать уже загруженный YAML-документ tours.yaml (без сверки с локациями)."""
    if not isinstance(document, dict):
        raise LocationsError(f"{source}: корневой объект должен быть отображением (mapping)")

    version = _require_int(document, "version", source)
    raw_tours = document.get("tours")
    if not isinstance(raw_tours, list) or not raw_tours:
        raise LocationsError(f"{source}: tours должен быть непустым списком")

    tours: dict[str, Tour] = {}
    for index, raw in enumerate(raw_tours):
        tour = _parse_tour(raw, index, source)
        if tour.id in tours:
            raise LocationsError(f"{source}: дублирующийся id тура {tour.id!r}")
        tours[tour.id] = tour

    return ToursFile(version=version, tours=tours)


def _parse_tour(raw: dict[str, Any], index: int, source: str) -> Tour:
    if not isinstance(raw, dict):
        raise LocationsError(f"{source}: tours[{index}] должен быть отображением")

    tour_id = _require_str(raw, "id", source, where=f"tours[{index}]")
    where = f"{source}: тур {tour_id!r}"

    name = raw.get("name")
    if not isinstance(name, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in name.items()
    ):
        raise LocationsError(f"{where}.name должен быть отображением language -> строка")

    default = raw.get("default", False)
    if not isinstance(default, bool):
        raise LocationsError(f"{where}.default должен быть булевым")

    raw_stops = raw.get("stops")
    if not isinstance(raw_stops, list) or not raw_stops:
        raise LocationsError(f"{where}.stops должен быть непустым списком")

    stops = [_parse_stop(raw_stop, i, where) for i, raw_stop in enumerate(raw_stops)]
    return Tour(id=tour_id, name=dict(name), default=default, stops=stops)


def _parse_stop(raw: Any, index: int, where: str) -> TourStop:
    if not isinstance(raw, dict):
        raise LocationsError(f"{where}.stops[{index}] должен быть отображением")

    location_id = _require_str(raw, "location_id", where, where=f"{where}.stops[{index}]")
    exhibit_id = _require_str(raw, "exhibit_id", where, where=f"{where}.stops[{index}]")

    dwell_s = raw.get("dwell_s")
    if isinstance(dwell_s, bool) or not isinstance(dwell_s, int) or dwell_s < 0:
        raise LocationsError(f"{where}.stops[{index}].dwell_s должен быть неотрицательным целым")

    mode = raw.get("mode")
    if mode not in VALID_MODES:
        raise LocationsError(
            f"{where}.stops[{index}].mode={mode!r} не входит в {sorted(VALID_MODES)}"
        )

    return TourStop(location_id=location_id, exhibit_id=exhibit_id, dwell_s=dwell_s, mode=mode)


def validate_tours(parsed: ToursFile, locations: LocationsFile) -> None:
    """Проверить, что все stop.location_id ссылаются на существующие локации."""
    for tour in parsed.tours.values():
        for stop_index, stop in enumerate(tour.stops):
            if stop.location_id not in locations.locations:
                raise LocationsError(
                    f"тур {tour.id!r}: stops[{stop_index}].location_id={stop.location_id!r} "
                    "не найден в locations.yaml"
                )


# -- фильтрация для location_server (design.md §1.1) -----------------------------


def is_visible(location: Location, category_filter: str) -> bool:
    """Можно ли отдавать локацию наружу для данного запроса category.

    is_public=false не отдаётся никогда -- кроме запроса ровно за
    служебной категорией (charging/service), которая совпадает с
    категорией самой локации. Так LLM не может случайно предложить
    посетителю подсобку через пустой/чужой category-фильтр, но система
    (например, поиск ближайшей зарядки) может явно её запросить.
    """
    if location.is_public:
        return True
    return category_filter in SERVICE_CATEGORIES and location.category == category_filter


def filter_near(
    locations: Sequence[Location], robot_x: float, robot_y: float, radius_m: float
) -> list[Location]:
    """Отсортировать по евклидову расстоянию от робота, отсечь дальше radius_m.

    Евклид, не путевое расстояние -- путевое расстояние считает
    route_planner через граф Route Server, дублировать эту зависимость
    в location_server незачем (design.md §1.1).
    """
    scored = [
        (math.hypot(location.pose.x - robot_x, location.pose.y - robot_y), location)
        for location in locations
    ]
    near = [(distance, location) for distance, location in scored if distance <= radius_m]
    near.sort(key=lambda item: item[0])
    return [location for _, location in near]


# -- общие хелперы -----------------------------------------------------------


def _read_yaml(path: str | Path) -> Any:
    location = Path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocationsError(f"{location}: не удалось прочитать файл: {exc}") from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise LocationsError(f"{location}: невалидный YAML: {exc}") from exc


def _require_str(raw: dict[str, Any], key: str, source: str, *, where: str | None = None) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        prefix = where or source
        raise LocationsError(f"{prefix}.{key} обязателен и должен быть непустой строкой")
    return value


def _require_int(raw: dict[str, Any], key: str, source: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocationsError(f"{source}.{key} обязателен и должен быть целым числом")
    return value
