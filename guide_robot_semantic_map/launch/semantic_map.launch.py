"""Запуск семантической карты: content_server и location_server.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию -- порядок
переходов должен быть управляемым извне (mission-оркестратором тура или
вручную через `ros2 lifecycle set`), а не зашитым в launch.

Зарегистрирован в guide_robot_supervisor как группа `semantic_map`
(config/supervisor.yaml, config/supervisor_slam.yaml, optional: true) --
тем же способом, что и `mission`/`voice` (см. guide_robot_mission_control/
launch/mission.launch.py). `lifecycle_manager_semantic_map` запускается
ВСЕГДА (не под условием) -- супервизор дёргает его `~/manage_nodes`
напрямую, сервис обязан существовать независимо от того, кто инициирует
STARTUP; см. `lifecycle_manager_safety` в
guide_robot_navigation/launch/common.launch.py, тот же паттерн. Параметр
`autostart` пробрасывается в сам lifecycle_manager как есть (default
"false" -- контракт супервизора: "Every lifecycle_manager referenced here
MUST have autostart: false"); autostart:=true остаётся для разработки и
ручной проверки без супервизора.

route_server (nav2_route) и route_planner (оценка маршрута, порядок остановок)
из стека убраны: миссия ведёт робота по точкам через Nav2 navigate_to_pose,
а их держание стоило ~0.3 ядра Jetson в простое.
"""

from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution

NODES = ["content_server", "location_server"]


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    params = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_semantic_map"), "config", "semantic_map.yaml"]
            ),
            description="Параметры наших нод (content_server/location_server)",
        ),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    nodes = [
        Node(
            package="guide_robot_semantic_map",
            executable=name,
            name=name,
            output="screen",
            # allow_substs: пути в semantic_map.yaml -- явные
            # $(find-pkg-share guide_robot_semantic_map)/config/*_innopark.*
            # (датасет реальной площадки), как в guide_robot_voice/voice.yaml.
            # Пустая строка тоже валидна -- сигнал коду резолвить путь
            # через ament_index самому (см. _resolve_path() в каждой ноде);
            # используется при развёртывании с данными вне пакета (design.md §2).
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
        # БЕЗ condition=IfCondition(autostart): супервизор дёргает
        # ~/manage_nodes этого менеджера напрямую (guide_robot_supervisor/
        # config/supervisor.yaml, группа "semantic_map"), сервис обязан
        # существовать вне зависимости от того, кто инициирует STARTUP.
        parameters=[
            {
                "use_sim_time": use_sim_time,
                # Пробрасываем launch-arg как есть (default "false") --
                # контракт супервизора: "Every lifecycle_manager referenced
                # here MUST have autostart: false".
                "autostart": autostart,
                "node_names": NODES,
                # content_server/location_server -- rclpy.lifecycle.LifecycleNode,
                # bond не создают (см. guide_robot_voice/launch/voice.launch.py);
                # 0.0 отключает проверку bond.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, GroupAction([*nodes, manager])])
