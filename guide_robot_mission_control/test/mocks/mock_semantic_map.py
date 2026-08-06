"""Моки трёх нод guide_robot_semantic_map (design §9.1, реконсиляция §0.5).

Реальная топология -- три отдельные lifecycle-ноды (content_server,
location_server, route_planner), каждая со своими `~/...`-сервисами
(guide_robot_semantic_map_design.md §1). Здесь три тонких ROS-обёртки
поверх одного общего хранилища фикстур: узлы под тестом (semantic_client.py
в narration_server/mission_fsm) обращаются к тем же именам сервисов
(`/content_server/get_exhibit_content` и т.д.), что и в проде -- никакой
подмены имён на стороне клиента для тестов не требуется.

Фикстуры настраиваются из теста напрямую через add_exhibit()/add_location()/
add_tour(), не через YAML-файл: content_io.py читает YAML, потому что для
content_server YAML -- источник истины контента; здесь источник истины --
сам тест, читать значения из YAML только для того, чтобы тут же положить
их в фикстуру, было бы уровнем косвенности без выгоды.
"""

from __future__ import annotations

from dataclasses import dataclass

from guide_robot_msgs.msg import Location as LocationMsg
from guide_robot_msgs.msg import Tour as TourMsg
from guide_robot_msgs.msg import TourStop as TourStopMsg
from guide_robot_msgs.srv import EstimateRoute, GetExhibitContent, ListLocations, ListTours
from rclpy.node import Node

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


class SemanticMapFixtures:
    """Общее хранилище данных для трёх нод-моков ниже. Не ROS-узел сама по себе."""

    def __init__(self) -> None:
        """Начать с пустых фикстур -- тест наполняет их явно."""
        self._exhibits: dict[tuple[str, str], _ExhibitFixture] = {}
        self._call_counts: dict[tuple[str, str], int] = {}
        self._rev_overrides: dict[tuple[str, str], tuple[int, str]] = {}
        self._locations: dict[str, LocationMsg] = {}
        self._tours: dict[str, TourMsg] = {}
        self.route_distance_m = 5.0
        self.route_duration_min = 2.0
        self.route_feasible = True

    def add_exhibit(
        self, exhibit_id: str, chunks: list[str], *, language: str = "ru", version: str = "v1"
    ) -> None:
        """Положить фикстуру контента для GetExhibitContent(exhibit_id, language)."""
        self._exhibits[(exhibit_id, language)] = _ExhibitFixture(
            chunks=list(chunks), version=version
        )

    def change_rev_after_n_calls(
        self, exhibit_id: str, *, after: int, new_version: str, language: str = "ru"
    ) -> None:
        """С вызова номер (after+1) GetExhibitContent начать отдавать new_version.

        Для test_resume_token.py-сценария "контент переиздан": narration_server
        обязан заметить смену version и перестроить resume с chunk_idx=0
        (resolve_resume, шаг 2), а не упасть и не молча продолжить со старого
        офсета.
        """
        self._rev_overrides[(exhibit_id, language)] = (after, new_version)

    def add_location(
        self,
        location_id: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        zone: str = "",
        category: str = "",
    ) -> None:
        """Положить фикстуру локации для ListLocations."""
        msg = LocationMsg(id=location_id, aliases=[], zone=zone, category=category, is_public=True)
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

    def get_exhibit_content(self, exhibit_id: str, language: str) -> tuple[list[str], str]:
        """Вернуть (chunks, version); ([], "") -- контента нет (design §1.3: не выдумывать)."""
        key = (exhibit_id, language or "ru")
        fixture = self._exhibits.get(key)
        if fixture is None:
            return [], ""
        count = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = count
        version = fixture.version
        override = self._rev_overrides.get(key)
        if override is not None:
            after, new_version = override
            if count > after:
                version = new_version
        return list(fixture.chunks), version

    def list_locations(self) -> list[LocationMsg]:
        """Все локации, положенные через add_location()."""
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

    def _handle(
        self, request: GetExhibitContent.Request, response: GetExhibitContent.Response
    ) -> GetExhibitContent.Response:
        chunks, version = self._fixtures.get_exhibit_content(request.exhibit_id, request.language)
        response.chunks = chunks
        response.version = version
        return response


class MockLocationServer(Node):
    """`~/list_locations` и `~/list_tours` поверх SemanticMapFixtures."""

    def __init__(
        self,
        fixtures: SemanticMapFixtures,
        node_name: str = "location_server",
        **node_kwargs: object,
    ) -> None:
        """Поднять сервисы list_locations/list_tours под именем узла location_server."""
        super().__init__(node_name, **node_kwargs)
        self._fixtures = fixtures
        self.create_service(ListLocations, "~/list_locations", self._handle_list_locations)
        self.create_service(ListTours, "~/list_tours", self._handle_list_tours)

    def _handle_list_locations(
        self, request: ListLocations.Request, response: ListLocations.Response
    ) -> ListLocations.Response:
        del request
        response.locations = self._fixtures.list_locations()
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
