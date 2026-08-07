"""Нода планирования маршрутов и оценки времени тура (design.md §1.2).

Клиент `nav2_msgs/action/ComputeRoute`, сервис `~/estimate_route`.
Единственная внешняя зависимость -- route_server: ни planner_server, ни
глобальная костмапа не нужны, пока не включён CostmapScorer.

Матрица направленных pairwise route_cost/distance_m между ВСЕМИ
локациями из locations.yaml считается один раз в on_activate и держится
в памяти (design.md §0.7: "30 локаций = 870 пар... итого секунды на
on_activate. Ни кэша, ни sha1 карты, ни assume_symmetric" -- то есть
именно синхронный прогрев при активации, не диск, не фон, не допущение
симметрии). Причина держать это в памяти, а не считать на каждый запрос:
пары между конфигурированными локациями статичны, пока не поменяется
граф, а `~/estimate_route` может вызываться многократно с разными
подмножествами `ids`.

Единственное, что нельзя закэшировать -- лега "текущая поза робота ->
кандидат на первую остановку": робот двигается, поэтому она считается
живым вызовом ComputeRoute (use_poses=true, use_start=false, TF) на
каждый запрос, для каждого id из request.ids.

Про route_cost и distance_m. route_cost -- скор Route Server (сумма
edge-скореров), используется только как целевая функция TSP. distance_m
в ответе -- отдельная величина, сумма евклидовых отрезков nav_msgs/Path.
Совпадают только при единственном DistanceScorer с weight=1.0 -- как
только добавится PenaltyScorer/SemanticScorer, разойдутся, и это
ожидаемо (design.md §1.2).

Про дефект контракта ComputeRoute.action в 1.1.20: константы кодов
ошибок объявлены, но поля под них в result нет (см. также phase 4 --
эмпирически подтверждено: дефект реальный, не гипотетический). Отличаем
"маршрута нет" от "сервер недоступен" по статусу цели, не по result.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import rclpy
import tf2_ros
from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
from guide_robot_msgs.msg import SystemEvent
from guide_robot_msgs.srv import EstimateRoute
from nav2_msgs.action import ComputeRoute
from rclpy.action import ActionClient
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future
from rclpy.time import Time

from guide_robot_semantic_map.lib.estimate import EstimateParams, estimate_duration_min
from guide_robot_semantic_map.lib.geometry import path_length_m, yaw_to_quaternion_xyzw
from guide_robot_semantic_map.lib.locations_io import Location, LocationsFile, load_locations
from guide_robot_semantic_map.lib.qos import QOS_SYSTEM_EVENT
from guide_robot_semantic_map.lib.tsp import solve_open_path
from guide_robot_semantic_map.service_guard import ServiceGuardMixin

_PACKAGE_NAME = "guide_robot_semantic_map"


@dataclass(frozen=True)
class _LegOutcome:
    """Результат одного вызова ComputeRoute, уже пережившего связь с сервером."""

    route_cost: float
    distance_m: float
    feasible: bool
    """False -- цель дошла до сервера, но статус не SUCCEEDED (маршрута нет)."""


class RoutePlannerNode(ServiceGuardMixin, LifecycleNode):
    """Lifecycle-нода `~/estimate_route`."""

    def __init__(self) -> None:
        """Объявить параметры. Данные и матрица грузятся в on_configure/on_activate."""
        super().__init__("route_planner")

        self.declare_parameter("locations_file", "")
        self.declare_parameter("nominal_speed_mps", 0.35)
        self.declare_parameter("crowd_factor", 0.7)
        self.declare_parameter("turn_penalty_s", 3.0)
        self.declare_parameter("tsp_time_budget_ms", 200.0)
        self.declare_parameter("compute_route_call_timeout_s", 2.0)
        self.declare_parameter("route_server_wait_timeout_s", 10.0)

        self._locations: LocationsFile | None = None
        self._route_cost: dict[tuple[str, str], float] = {}
        self._distance_m: dict[tuple[str, str], float] = {}
        self._tf_buffer = None
        self._tf_listener = None
        self._active = False
        self._event_pub = None
        self._stage = "инициализация"

        self._cb_action = ReentrantCallbackGroup()
        self._cb_service = MutuallyExclusiveCallbackGroup()
        self._action_client = ActionClient(
            self, ComputeRoute, "/compute_route", callback_group=self._cb_action
        )

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить locations.yaml, поднять интерфейсы. Матрица -- в on_activate.

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
        self._stage = "загрузка локаций"
        locations_file = str(self.get_parameter("locations_file").value)
        if not locations_file:
            locations_file = (
                f"{get_package_share_directory(_PACKAGE_NAME)}/config/locations.yaml"
            )
        self._locations = load_locations(locations_file)

        self._stage = "TF"
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        self._stage = "интерфейсы ROS"
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )
        self._service = self.create_service(
            EstimateRoute,
            "~/estimate_route",
            self._on_estimate_route,
            callback_group=self._cb_service,
        )

        self._stage = "готово"
        self.get_logger().info(
            f"route_planner сконфигурирован: {len(self._locations.locations)} локаций из "
            f"{locations_file}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Дождаться route_server и (при первой активации) прогреть матрицу пар."""
        try:
            self._stage = "ожидание route_server"
            timeout = float(self.get_parameter("route_server_wait_timeout_s").value)
            if not self._action_client.wait_for_server(timeout_sec=timeout):
                raise RuntimeError(
                    f"route_server (/compute_route) недоступен за {timeout} с"
                )
            if not self._route_cost:
                self._stage = "прогрев матрицы"
                self._warm_up_matrix()
        except Exception as error:
            self.get_logger().error(f"activate не удался на шаге '{self._stage}': {error}")
            return TransitionCallbackReturn.FAILURE
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Перестать отвечать на сервис явным отказом, не молчанием. Матрица остаётся в памяти."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить локации и матрицу -- следующий configure загрузит их заново."""
        del state
        self._locations = None
        self._route_cost = {}
        self._distance_m = {}
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- прогрев статической матрицы (on_activate) ---------------------------------

    def _warm_up_matrix(self) -> None:
        assert self._locations is not None
        ids = list(self._locations.locations.keys())
        total_pairs = len(ids) * (len(ids) - 1)
        self.get_logger().info(
            f"route_planner: прогрев матрицы -- {len(ids)} локаций, {total_pairs} "
            "направленных пар..."
        )
        started = time.monotonic()
        infeasible = 0
        for start_id in ids:
            for goal_id in ids:
                if start_id == goal_id:
                    continue
                infeasible += self._warm_up_pair(start_id, goal_id)
        elapsed_ms = (time.monotonic() - started) * 1e3
        self.get_logger().info(
            f"route_planner: матрица прогрета за {elapsed_ms:.0f} мс, "
            f"{total_pairs - infeasible}/{total_pairs} пар маршрутизируемы"
        )

    def _warm_up_pair(self, start_id: str, goal_id: str) -> int:
        assert self._locations is not None
        start_node = self._locations.locations[start_id].graph_node
        goal_node = self._locations.locations[goal_id].graph_node
        outcome = self._call_compute_route(start_node=start_node, goal_node=goal_node)
        if outcome is None:
            self.get_logger().error(
                f"route_planner: route_server не ответил на паре {start_id}->{goal_id}, "
                "кэширую как недостижимую"
            )
            self._route_cost[(start_id, goal_id)] = math.inf
            self._distance_m[(start_id, goal_id)] = 0.0
            return 1
        self._route_cost[(start_id, goal_id)] = outcome.route_cost
        self._distance_m[(start_id, goal_id)] = outcome.distance_m
        return 0 if outcome.feasible else 1

    # -- ~/estimate_route -----------------------------------------------------

    def _on_estimate_route(
        self, request: EstimateRoute.Request, response: EstimateRoute.Response
    ) -> EstimateRoute.Response:
        if not self._require_active("estimate_route"):
            return response
        assert self._locations is not None

        ids = list(request.ids)
        unknown = [i for i in ids if i not in self._locations.locations]
        if unknown:
            self.get_logger().warning(f"estimate_route: неизвестные id локаций: {unknown}")
            return self._infeasible_response(response, ids)

        if not ids:
            response.ordered_ids = []
            response.distance_m = 0.0
            response.duration_min = 0.0
            response.feasible = True
            return response

        if not self._tf_available():
            detail = "TF map->base_link недоступен, estimate_route отдаёт вход как есть"
            self.get_logger().warning(f"estimate_route: {detail}")
            self._publish_system_event(
                "semantic_map.estimate_route_tf_unavailable", SystemEvent.WARN, detail
            )
            return self._infeasible_response(response, ids)

        start_legs = [self._first_leg(location_id) for location_id in ids]

        n = len(ids)
        costs = [[0.0] * n for _ in range(n)]
        pair_distance = [[0.0] * n for _ in range(n)]
        pair_feasible = [[True] * n for _ in range(n)]
        for i, a in enumerate(ids):
            for j, b in enumerate(ids):
                if i == j:
                    continue
                cost = self._route_cost.get((a, b), math.inf)
                costs[i][j] = cost
                pair_distance[i][j] = self._distance_m.get((a, b), 0.0)
                pair_feasible[i][j] = math.isfinite(cost)

        if request.optimize:
            start_costs = [leg.route_cost for leg in start_legs]
            budget = float(self.get_parameter("tsp_time_budget_ms").value)
            order = solve_open_path(start_costs, costs, time_budget_ms=budget).order
        else:
            order = list(range(n))

        first = order[0]
        leg_distances = [start_legs[first].distance_m]
        feasible = start_legs[first].feasible
        for k in range(1, n):
            prev_i, cur_i = order[k - 1], order[k]
            leg_distances.append(pair_distance[prev_i][cur_i])
            feasible = feasible and pair_feasible[prev_i][cur_i]

        params = EstimateParams(
            nominal_speed_mps=float(self.get_parameter("nominal_speed_mps").value),
            crowd_factor=float(self.get_parameter("crowd_factor").value),
            turn_penalty_s=float(self.get_parameter("turn_penalty_s").value),
        )

        response.ordered_ids = [ids[i] for i in order]
        response.distance_m = sum(leg_distances)
        response.duration_min = estimate_duration_min(leg_distances, [0.0] * n, params=params)
        response.feasible = feasible
        return response

    def _infeasible_response(
        self, response: EstimateRoute.Response, ids: list[str]
    ) -> EstimateRoute.Response:
        response.ordered_ids = ids
        response.distance_m = 0.0
        response.duration_min = 0.0
        response.feasible = False
        return response

    def _first_leg(self, location_id: str) -> _LegOutcome:
        assert self._locations is not None
        goal_pose = self._to_pose_stamped(self._locations.locations[location_id])
        outcome = self._call_compute_route(goal_pose=goal_pose)
        if outcome is None:
            self.get_logger().error(
                f"estimate_route: route_server не ответил на леге старт->{location_id}"
            )
            return _LegOutcome(route_cost=math.inf, distance_m=0.0, feasible=False)
        return outcome

    # -- ComputeRoute: синхронный вызов из потока сервиса/lifecycle -----------------

    def _call_compute_route(
        self,
        *,
        start_node: int = 0,
        goal_node: int = 0,
        goal_pose: PoseStamped | None = None,
    ) -> _LegOutcome | None:
        """Один вызов ComputeRoute. None -- сервер не ответил (связь, не маршрут).

        goal_pose задан -> use_poses=true (лега "текущая поза -> цель", TF
        внутри route_server, design.md §0.4/§1.2, подтверждено в фазе 4).
        Иначе -- use_poses=false, start_node/goal_node -- graph_node из
        locations.yaml (лега матрицы между сконфигурированными точками).

        Вызывается и из on_activate (прогрев), и из колбэка сервиса --
        обе стороны блокируются на threading.Event, а не на
        rclpy.spin_until_future_complete: колбэки самого ActionClient
        обрабатываются в отдельной ReentrantCallbackGroup, для чего нужен
        MultiThreadedExecutor с хотя бы одним свободным потоком (см. main()).
        """
        goal = ComputeRoute.Goal()
        goal.use_start = False
        if goal_pose is not None:
            goal.use_poses = True
            goal.goal = goal_pose
        else:
            goal.use_poses = False
            goal.start_id = start_node
            goal.goal_id = goal_node

        timeout = float(self.get_parameter("compute_route_call_timeout_s").value)

        send_future = self._action_client.send_goal_async(goal)
        if not self._wait_for_future(send_future, timeout):
            self.get_logger().warning("ComputeRoute: таймаут отправки цели")
            return None
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            self.get_logger().warning("ComputeRoute: цель отклонена сервером")
            return None

        result_future = goal_handle.get_result_async()
        if not self._wait_for_future(result_future, timeout):
            self.get_logger().warning("ComputeRoute: таймаут результата")
            return None
        result_response = result_future.result()
        if result_response is None:
            return None

        if result_response.status != GoalStatus.STATUS_SUCCEEDED:
            return _LegOutcome(route_cost=math.inf, distance_m=0.0, feasible=False)

        result = result_response.result
        points = [(pose.pose.position.x, pose.pose.position.y) for pose in result.path.poses]
        return _LegOutcome(
            route_cost=result.route.route_cost,
            distance_m=path_length_m(points),
            feasible=True,
        )

    def _wait_for_future(self, future: Future, timeout_s: float) -> bool:
        """Дождаться завершения future. Возвращает True, если успело за timeout_s.

        threading.Event, не rclpy.spin_until_future_complete: этот метод
        сам исполняется на потоке executor'а (сервисный или lifecycle
        колбэк), а spin_until_future_complete внутри уже занятого потока
        MultiThreadedExecutor рискует дедлоком, если свободных потоков
        не осталось. add_done_callback гарантированно вызывается на любом
        потоке, который исполняет ReentrantCallbackGroup ActionClient'а.
        """
        event = threading.Event()
        future.add_done_callback(lambda _future: event.set())
        return event.wait(timeout=timeout_s)

    # -- TF и конвертация поз -----------------------------------------------

    def _tf_available(self) -> bool:
        """Доступен ли TF map->base_link прямо сейчас.

        route_server при use_start=false игнорирует переданное поле
        `start` целиком и делает TF-подстановку сам (design.md §0.4,
        подтверждено в фазе 4 -- пустой `start` в запросе не мешает
        серверу найти позу). Поэтому здесь не нужна реальная поза,
        только факт: если TF нет, все N лег "старт -> кандидат" провалятся
        одинаково -- дешевле отловить это одним lookup'ом заранее и
        отдать `ordered_ids` как есть (design.md §0.4), чем гонять N
        заведомо провальных ComputeRoute и получить произвольный порядок
        от TSP на всех-бесконечных start_costs.
        """
        assert self._locations is not None
        assert self._tf_buffer is not None
        try:
            self._tf_buffer.lookup_transform(self._locations.frame_id, "base_link", Time())
        except (
            tf2_ros.LookupException,
            tf2_ros.ConnectivityException,
            tf2_ros.ExtrapolationException,
        ) as error:
            self.get_logger().warning(
                f"TF {self._locations.frame_id}->base_link недоступен: {error}"
            )
            return False
        return True

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


def main(args: list[str] | None = None) -> None:
    """Точка входа.

    MultiThreadedExecutor обязателен: ComputeRoute вызывается синхронно
    изнутри колбэков сервиса и lifecycle, а их ответы обрабатываются в
    отдельной callback-группе -- см. докстринг _wait_for_future.
    """
    rclpy.init(args=args)
    node = RoutePlannerNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
