"""Запуск семантической карты: route_server (nav2_route) + три наши ноды.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию -- та же
причина, что и в guide_robot_voice/guide_robot_llm: порядок переходов
должен быть управляемым извне (mission-оркестратором тура или вручную
через `ros2 lifecycle set`), а не зашитым в launch. guide_robot_semantic_map,
как и voice/llm, НЕ зарегистрирован в guide_robot_supervisor/config/
supervisor.yaml -- тот файл жёстко про safety-critical нав-стек
(safety/localization/navigation), а voice/llm/semantic_map -- служебный
слой поверх него, которым supervisor сознательно не владеет.

autostart:=true поднимает всё сразу через lifecycle_manager -- удобно
для разработки и ручной проверки, не рабочий режим на роботе.

Порядок bring-up (критичен при autostart:=true, подтверждено в фазах 4-5):
  route_server -> content_server -> location_server -> route_planner
route_planner на activate ждёт /compute_route (route_server) и сразу
прогревает матрицу пар -- без активного route_server activate провалится.
content_server/location_server от route_server не зависят, порядок между
собой не важен.

route_server -- чужой пакет (nav2_route), но в репозитории его больше
никто не запускает: graph.geojson живёт в config/ этого пакета, и
route_planner -- единственный потребитель. Поднимается здесь же, а не
в guide_robot_navigation, чтобы владелец данных запускал их потребителя.
"""

from ament_index_python.packages import get_package_share_directory
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription

NODES = ["content_server", "location_server", "route_planner"]

# route_server первым -- route_planner требует его активным до своего
# on_activate (прогрев матрицы пар, design.md §0.7/§1.2). content_server
# и location_server от route_server не зависят, порядок между собой
# не важен.
BRINGUP_ORDER = ["route_server", "content_server", "location_server", "route_planner"]


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    pkg_share = get_package_share_directory("guide_robot_semantic_map")

    params = LaunchConfiguration("params_file")
    graph_file = LaunchConfiguration("graph_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_semantic_map"), "config", "semantic_map.yaml"]
            ),
            description="Параметры трёх наших нод (content_server/location_server/route_planner)",
        ),
        DeclareLaunchArgument(
            "graph_file",
            default_value=f"{pkg_share}/config/graph.geojson",
            description="Граф для route_server -- владелец данных, не launch-аргумент",
        ),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    route_server = Node(
        package="nav2_route",
        executable="route_server",
        name="route_server",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "graph_filepath": graph_file,
                "graph_file_loader": "GeoJsonGraphFileLoader",
            }
        ],
        arguments=["--ros-args", "--log-level", log_level],
    )

    nodes = [
        Node(
            package="guide_robot_semantic_map",
            executable=name,
            name=name,
            output="screen",
            # allow_substs: пути в semantic_map.yaml пока пустые (резолвятся
            # в коде через ament_index), но при развёртывании с данными вне
            # пакета (design.md §2) сюда придёт $(find-pkg-share ...), как
            # в guide_robot_voice/voice.yaml.
            parameters=[ParameterFile(params, allow_substs=True), {"use_sim_time": use_sim_time}],
            arguments=["--ros-args", "--log-level", log_level],
        )
        for name in NODES
    ]

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_semantic_map",
        output="screen",
        condition=IfCondition(autostart),
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "autostart": True,
                "node_names": BRINGUP_ORDER,
                # route_server -- C++ nav2_util::LifecycleNode, создаёт bond;
                # content_server/location_server/route_planner -- обычные
                # rclpy.lifecycle.LifecycleNode, bond не создают (см.
                # guide_robot_voice/launch/voice.launch.py). 0.0 отключает
                # проверку bond для группы целиком -- смешанный состав
                # менеджера иначе не поднять единым node_names.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, GroupAction([route_server, *nodes, manager])])
