"""Нода локаций и туров (design.md §1.1).

Три сервиса: ~/list_locations, ~/resolve_location (fuzzy-резолв алиасов),
~/list_tours. Читает locations.yaml, tours.yaml и graph.geojson -- граф
нужен только для валидации ссылок graph_node на configure, сами сервисы
его не используют (топологию и path-расстояния считает route_planner).

Про Tour.msg.duration_min_estimate. Оценка длительности тура требует
путевых расстояний между остановками -- это lib/estimate.py и
lib/tsp.py, которые работают на результатах ComputeRoute, а location_server
намеренно не зависит от route_server/Nav2 (design.md §1.1: только TF).
Поле в ответе всегда 0 -- за реальной оценкой mission обращается к
route_planner (`~/estimate_route`) со списком stop.location_id тура.

Про near_only при недоступном TF. EstimateRoute (design.md §0.4) в этом
случае отдаёт feasible=false -- у ListLocations такого канала нет
(ListLocations.srv не содержит поля-признака ошибки), поэтому падать
обратно на пустой список было бы хуже, чем отдать нефильтрованный:
локации существуют и валидны независимо от TF, только расстояние до них
посчитать не вышло. Решение: лог + SystemEvent(WARN), а список --
как без near_only, без сортировки по расстоянию.
"""

from __future__ import annotations

import rclpy
import tf2_ros
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
from guide_robot_msgs.msg import Location as LocationMsg
from guide_robot_msgs.msg import SystemEvent
from guide_robot_msgs.msg import Tour as TourMsg
from guide_robot_msgs.msg import TourStop as TourStopMsg
from guide_robot_msgs.srv import ListLocations, ListTours, ResolveLocation
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.time import Time

from guide_robot_semantic_map.lib.content_io import pick_language
from guide_robot_semantic_map.lib.geometry import yaw_to_quaternion_xyzw
from guide_robot_semantic_map.lib.graph_io import load_graph
from guide_robot_semantic_map.lib.locations_io import (
    Location,
    LocationsFile,
    ToursFile,
    filter_near,
    is_visible,
    load_locations,
    load_tours,
    validate_graph_links,
    validate_tours,
)
from guide_robot_semantic_map.lib.matching import is_confident, resolve
from guide_robot_semantic_map.lib.qos import QOS_SYSTEM_EVENT
from guide_robot_semantic_map.service_guard import ServiceGuardMixin

_PACKAGE_NAME = "guide_robot_semantic_map"


class LocationServerNode(ServiceGuardMixin, LifecycleNode):
    """Lifecycle-нода `~/list_locations`, `~/resolve_location`, `~/list_tours`."""

    def __init__(self) -> None:
        """Объявить параметры. Данные грузятся в on_configure."""
        super().__init__("location_server")

        self.declare_parameter("locations_file", "")
        self.declare_parameter("tours_file", "")
        self.declare_parameter("graph_file", "")
        self.declare_parameter("default_language", "ru")
        self.declare_parameter("near_radius_m", 5.0)

        self._locations: LocationsFile | None = None
        self._tours: ToursFile | None = None
        self._tf_buffer: tf2_ros.Buffer | None = None
        self._tf_listener: tf2_ros.TransformListener | None = None
        self._active = False
        self._event_pub = None
        self._stage = "инициализация"

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить и провалидировать данные. Любая ошибка -- отказ активации.

        Тело целиком в try: исключение из колбэка перехода lifecycle
        машина состояний поглощает без объяснений наружу -- причину
        логируем сами, по self._stage.
        """
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался на шаге '{self._stage}': {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        # graph.geojson нужен только чтобы провалидировать graph_node --
        # деталями графа location_server не пользуется за пределами
        # этой проверки (топологию считает route_planner).
        self._stage = "загрузка графа (только для валидации ссылок)"
        graph = load_graph(self._resolve_path("graph_file", "config/graph.geojson"))

        self._stage = "загрузка локаций"
        locations_file = self._resolve_path("locations_file", "config/locations.yaml")
        self._locations = load_locations(locations_file)
        validate_graph_links(self._locations, set(graph.nodes))

        self._stage = "загрузка туров"
        tours_file = self._resolve_path("tours_file", "config/tours.yaml")
        self._tours = load_tours(tours_file)
        validate_tours(self._tours, self._locations)

        self._stage = "TF"
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        self._stage = "интерфейсы ROS"
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )
        self._list_locations_srv = self.create_service(
            ListLocations, "~/list_locations", self._on_list_locations
        )
        self._resolve_location_srv = self.create_service(
            ResolveLocation, "~/resolve_location", self._on_resolve_location
        )
        self._list_tours_srv = self.create_service(
            ListTours, "~/list_tours", self._on_list_tours
        )

        self._stage = "готово"
        self.get_logger().info(
            f"location_server сконфигурирован: {len(self._locations.locations)} локаций, "
            f"{len(self._tours.tours)} туров из {locations_file}, {tours_file}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Начать отвечать на сервисы."""
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Перестать отвечать на сервисы явным отказом, не молчанием."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить загруженные данные."""
        del state
        self._locations = None
        self._tours = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- ~/list_locations -----------------------------------------------

    def _on_list_locations(
        self, request: ListLocations.Request, response: ListLocations.Response
    ) -> ListLocations.Response:
        if not self._require_active("list_locations"):
            return response
        assert self._locations is not None

        candidates = [
            location
            for location in self._locations.locations.values()
            if (not request.zone or location.zone == request.zone)
            and (not request.category or location.category == request.category)
            and is_visible(location, request.category)
        ]

        if request.near_only:
            candidates = self._apply_near_filter(candidates)

        response.locations = [
            self._to_location_msg(location) for location in candidates
        ]
        return response

    def _apply_near_filter(self, candidates: list[Location]) -> list[Location]:
        robot_xy = self._lookup_robot_xy()
        if robot_xy is None:
            detail = (
                "near_only=true, но TF map->base_link недоступен; "
                "фильтр по расстоянию пропущен"
            )
            self.get_logger().warning(f"list_locations: {detail}")
            self._publish_system_event(
                "semantic_map.near_only_tf_unavailable", SystemEvent.WARN, detail
            )
            return candidates
        robot_x, robot_y = robot_xy
        radius_m = float(self.get_parameter("near_radius_m").value)
        return filter_near(candidates, robot_x, robot_y, radius_m)

    def _lookup_robot_xy(self) -> tuple[float, float] | None:
        assert self._locations is not None
        assert self._tf_buffer is not None
        try:
            transform = self._tf_buffer.lookup_transform(
                self._locations.frame_id, "base_link", Time()
            )
        except (
            tf2_ros.LookupException,
            tf2_ros.ConnectivityException,
            tf2_ros.ExtrapolationException,
        ) as error:
            self.get_logger().warning(
                f"TF {self._locations.frame_id}->base_link недоступен: {error}"
            )
            return None
        return transform.transform.translation.x, transform.transform.translation.y

    # -- ~/resolve_location -----------------------------------------------

    def _on_resolve_location(
        self, request: ResolveLocation.Request, response: ResolveLocation.Response
    ) -> ResolveLocation.Response:
        if not self._require_active("resolve_location"):
            return response
        assert self._locations is not None

        # Только публичные локации -- design.md §1.1: "чтобы LLM не мог
        # случайно предложить посетителю подсобку". У ResolveLocation.srv,
        # в отличие от ListLocations, нет category-исключения для
        # намеренного запроса служебной локации.
        alias_pool = {
            location.id: location.aliases
            for location in self._locations.locations.values()
            if location.is_public
        }
        matches = resolve(
            request.query,
            alias_pool,
            language=request.language,
            max_results=request.max_results,
        )

        response.candidates = [
            self._to_location_msg(self._locations.locations[match.location_id])
            for match in matches
        ]
        response.scores = [match.score for match in matches]
        response.confident = is_confident(list(response.scores))
        return response

    # -- ~/list_tours -----------------------------------------------------

    def _on_list_tours(
        self, request: ListTours.Request, response: ListTours.Response
    ) -> ListTours.Response:
        if not self._require_active("list_tours"):
            return response
        assert self._tours is not None

        default_language = str(self.get_parameter("default_language").value)
        tours: list[TourMsg] = []
        for tour in self._tours.tours.values():
            language = pick_language(set(tour.name), request.language, default_language)
            if language is None:
                self.get_logger().warning(
                    f"list_tours: тур {tour.id!r} недоступен на языке "
                    f"{request.language!r} (default {default_language!r})"
                )
                continue
            tours.append(
                TourMsg(
                    id=tour.id,
                    name=tour.name[language],
                    duration_min_estimate=0,
                    stops=[
                        TourStopMsg(
                            location_id=stop.location_id,
                            exhibit_id=stop.exhibit_id,
                            dwell_s=stop.dwell_s,
                            mode=stop.mode,
                        )
                        for stop in tour.stops
                    ],
                )
            )
        response.tours = tours
        return response

    # -- общие хелперы -----------------------------------------------------

    def _to_location_msg(self, location: Location) -> LocationMsg:
        assert self._locations is not None
        msg = LocationMsg()
        msg.id = location.id
        msg.aliases = [alias for aliases in location.aliases.values() for alias in aliases]
        msg.pose = self._to_pose_stamped(location)
        msg.zone = location.zone
        msg.category = location.category
        msg.is_public = location.is_public
        return msg

    def _to_pose_stamped(self, location: Location) -> PoseStamped:
        assert self._locations is not None
        pose = PoseStamped()
        pose.header.frame_id = self._locations.frame_id
        pose.pose.position.x = location.pose.x
        pose.pose.position.y = location.pose.y
        qx, qy, qz, qw = yaw_to_quaternion_xyzw(location.pose.yaw)
        pose.pose.orientation.x = qx
        pose.pose.orientation.y = qy
        pose.pose.orientation.z = qz
        pose.pose.orientation.w = qw
        return pose

    def _resolve_path(self, param_name: str, relative_default: str) -> str:
        value = str(self.get_parameter(param_name).value)
        if value:
            return value
        return f"{get_package_share_directory(_PACKAGE_NAME)}/{relative_default}"


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = LocationServerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
