"""Моки трёх нод guide_robot_semantic_map (content_server/location_server/route_planner).

Копия guide_robot_mission_control/test/mocks/mock_semantic_map.py -- см.
докстринг sim_clock.py про причину копии, не импорта.
"""

from __future__ import annotations

from dataclasses import dataclass

from rclpy.node import Node

from guide_robot_msgs.msg import ContentHit as ContentHitMsg
from guide_robot_msgs.msg import Location as LocationMsg
from guide_robot_msgs.msg import Tour as TourMsg
from guide_robot_msgs.msg import TourStop as TourStopMsg
from guide_robot_msgs.srv import (
    EstimateRoute,
    GetExhibitContent,
    ListLocations,
    ListTours,
    ResolveLocation,
    SearchContent,
)

__all__ = [
    "MockContentServer",
    "MockLocationServer",
    "MockRoutePlanner",
    "SemanticMapFixtures",
]


@dataclass
class _ExhibitFixture:
    chunks: list[str]
    version: str
    title: str = ""
    kind: str = "exhibit"
    chunk_ids: list[str] | None = None


class SemanticMapFixtures:
    """Общее хранилище данных для трёх нод-моков ниже. Не ROS-узел сама по себе."""

    def __init__(self) -> None:
        """Начать с пустых фикстур -- тест наполняет их явно."""
        self._exhibits: dict[tuple[str, str], _ExhibitFixture] = {}
        self._call_counts: dict[tuple[str, str], int] = {}
        self._locations: dict[str, LocationMsg] = {}
        self._tours: dict[str, TourMsg] = {}
        self.route_distance_m = 5.0
        self.route_duration_min = 2.0
        self.route_feasible = True
        self.search_hits: list[ContentHitMsg] = []
        self.resolve_candidates: list[LocationMsg] = []
        self.resolve_scores: list[float] = []
        self.resolve_confident = False

    def add_exhibit(
        self,
        exhibit_id: str,
        chunks: list[str],
        *,
        language: str = "ru",
        version: str = "v1",
        title: str = "",
        kind: str = "exhibit",
        chunk_ids: list[str] | None = None,
    ) -> None:
        """Положить фикстуру контента для GetExhibitContent(exhibit_id, language).

        `chunk_ids` по умолчанию -- "c0", "c1", ... по числу chunks (тот же
        формат id, что реальный content_server отдаёт параллельно chunks).
        """
        self._exhibits[(exhibit_id, language)] = _ExhibitFixture(
            chunks=list(chunks),
            version=version,
            title=title,
            kind=kind,
            chunk_ids=list(chunk_ids) if chunk_ids is not None else [
                f"c{i}" for i in range(len(chunks))
            ],
        )

    def set_search_hits(self, hits: list[dict]) -> None:
        """Задать фиксированный результат SearchContent для всех запросов (как route_estimate).

        `hits` -- список словарей {content_id, kind, title, chunk_id, text,
        score, version}; отсутствующие ключи -- default пустая строка/0.0.
        """
        self.search_hits = [
            ContentHitMsg(
                content_id=hit.get("content_id", ""),
                kind=hit.get("kind", ""),
                title=hit.get("title", ""),
                chunk_id=hit.get("chunk_id", ""),
                text=hit.get("text", ""),
                score=hit.get("score", 0.0),
                version=hit.get("version", ""),
            )
            for hit in hits
        ]

    def set_resolve_result(
        self, candidates: list[str], scores: list[float], *, confident: bool = False
    ) -> None:
        """Задать фиксированный результат ResolveLocation -- candidates по id из add_location()."""
        self.resolve_candidates = [self._locations[loc_id] for loc_id in candidates]
        self.resolve_scores = list(scores)
        self.resolve_confident = confident

    def add_location(
        self,
        location_id: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        zone: str = "",
        category: str = "",
        is_public: bool = True,
    ) -> None:
        """Положить фикстуру локации для ListLocations."""
        msg = LocationMsg(
            id=location_id, aliases=[], zone=zone, category=category, is_public=is_public
        )
        msg.pose.header.frame_id = "map"
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        self._locations[location_id] = msg

    def add_tour(
        self,
        tour_id: str,
        name: str,
        stops: list[tuple[str, str, int, str]],
        *,
        duration_min_estimate: int = 0,
    ) -> None:
        """Положить фикстуру тура. stops -- (location_id, exhibit_id, dwell_s, mode)."""
        tour = TourMsg(id=tour_id, name=name, duration_min_estimate=duration_min_estimate)
        tour.stops = [
            TourStopMsg(location_id=loc, exhibit_id=exh, dwell_s=dwell, mode=mode)
            for loc, exh, dwell, mode in stops
        ]
        self._tours[tour_id] = tour

    def set_route_estimate(
        self, *, distance_m: float, duration_min: float, feasible: bool = True
    ) -> None:
        """Задать фиксированный результат EstimateRoute для всех запросов."""
        self.route_distance_m = distance_m
        self.route_duration_min = duration_min
        self.route_feasible = feasible

    # -- обработчики, дёргаются нодами-обёртками ниже --------------------

    def get_exhibit_content(self, exhibit_id: str, language: str) -> _ExhibitFixture | None:
        """Вернуть фикстуру целиком; `None` -- контента нет."""
        key = (exhibit_id, language or "ru")
        fixture = self._exhibits.get(key)
        if fixture is None:
            return None
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        return fixture

    def list_locations(self) -> list[LocationMsg]:
        """Все локации add_location() без учёта is_public/zone -- фильтр в MockLocationServer."""
        return list(self._locations.values())

    def list_tours(self) -> list[TourMsg]:
        """Все туры, положенные через add_tour()."""
        return list(self._tours.values())


class MockContentServer(Node):
    """`~/get_exhibit_content` поверх SemanticMapFixtures."""

    def __init__(
        self,
        fixtures: SemanticMapFixtures,
        node_name: str = "content_server",
        **node_kwargs: object,
    ) -> None:
        """Поднять сервис get_exhibit_content под именем узла content_server (как в проде)."""
        super().__init__(node_name, **node_kwargs)
        self._fixtures = fixtures
        self.create_service(GetExhibitContent, "~/get_exhibit_content", self._handle)
        self.create_service(SearchContent, "~/search_content", self._handle_search)

    def _handle(
        self, request: GetExhibitContent.Request, response: GetExhibitContent.Response
    ) -> GetExhibitContent.Response:
        fixture = self._fixtures.get_exhibit_content(request.exhibit_id, request.language)
        if fixture is None:
            return response
        response.chunks = fixture.chunks
        response.chunk_ids = fixture.chunk_ids or []
        response.title = fixture.title
        response.kind = fixture.kind
        response.version = fixture.version
        return response

    def _handle_search(
        self, request: SearchContent.Request, response: SearchContent.Response
    ) -> SearchContent.Response:
        """Канонический результат из `set_search_hits()` -- без реального ранжирования.

        Как `MockRoutePlanner`: тест задаёт фиксированный ответ, содержимое
        запроса (`query`/`location_ids`/`kinds`) здесь не используется --
        реальный BM25-поиск живёт в guide_robot_semantic_map, не копируется.
        """
        del request
        response.hits = list(self._fixtures.search_hits)
        return response


class MockLocationServer(Node):
    """`~/list_locations` и `~/list_tours` поверх SemanticMapFixtures.

    В отличие от реального location_server (guide_robot_semantic_map/
    lib/locations_io.py:is_visible), фильтр is_public/zone/category здесь
    воспроизведён -- tool_broker's whitelist-логика (llm_plam.md §0.4)
    полагается ровно на это поведение, а не только на "пустая category ->
    всё публичное" по счастливой случайности мока.
    """

    def __init__(
        self,
        fixtures: SemanticMapFixtures,
        node_name: str = "location_server",
        **node_kwargs: object,
    ) -> None:
        """Поднять сервисы list_locations/list_tours/resolve_location под location_server."""
        super().__init__(node_name, **node_kwargs)
        self._fixtures = fixtures
        self.create_service(ListLocations, "~/list_locations", self._handle_list_locations)
        self.create_service(ListTours, "~/list_tours", self._handle_list_tours)
        self.create_service(ResolveLocation, "~/resolve_location", self._handle_resolve_location)

    def _handle_list_locations(
        self, request: ListLocations.Request, response: ListLocations.Response
    ) -> ListLocations.Response:
        candidates = self._fixtures.list_locations()
        if request.zone:
            candidates = [loc for loc in candidates if loc.zone == request.zone]
        if request.category:
            candidates = [loc for loc in candidates if loc.category == request.category]
        else:
            candidates = [loc for loc in candidates if loc.is_public]
        response.locations = candidates
        return response

    def _handle_resolve_location(
        self, request: ResolveLocation.Request, response: ResolveLocation.Response
    ) -> ResolveLocation.Response:
        """Канонический результат из `set_resolve_result()` -- без реального fuzzy-матча."""
        del request
        response.candidates = list(self._fixtures.resolve_candidates)
        response.scores = list(self._fixtures.resolve_scores)
        response.confident = self._fixtures.resolve_confident
        return response

    def _handle_list_tours(
        self, request: ListTours.Request, response: ListTours.Response
    ) -> ListTours.Response:
        del request
        response.tours = self._fixtures.list_tours()
        return response


class MockRoutePlanner(Node):
    """`~/estimate_route` поверх SemanticMapFixtures."""

    def __init__(
        self,
        fixtures: SemanticMapFixtures,
        node_name: str = "route_planner",
        **node_kwargs: object,
    ) -> None:
        """Поднять сервис estimate_route под именем узла route_planner (как в проде)."""
        super().__init__(node_name, **node_kwargs)
        self._fixtures = fixtures
        self.create_service(EstimateRoute, "~/estimate_route", self._handle)

    def _handle(
        self, request: EstimateRoute.Request, response: EstimateRoute.Response
    ) -> EstimateRoute.Response:
        response.ordered_ids = list(request.ids)
        response.distance_m = self._fixtures.route_distance_m
        response.duration_min = self._fixtures.route_duration_min
        response.feasible = self._fixtures.route_feasible
        return response
