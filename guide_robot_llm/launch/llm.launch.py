"""Запуск guide_robot_llm: tool_broker + dialog_agent + interaction_log.

По образцу guide_robot_mission_control/launch/mission.launch.py: ноды --
lifecycle и НЕ поднимаются автоматически по умолчанию -- порядок переходов
управляется извне (супервизором или вручную через `ros2 lifecycle set`).
`lifecycle_manager_llm` запускается ВСЕГДА (не под условием autostart) --
тот же контракт, что у `lifecycle_manager_mission`: сервис `~/manage_nodes`
обязан существовать независимо от того, кто решит вызвать STARTUP.
`BRINGUP_ORDER` -- `tool_broker` ПЕРЕД `dialog_agent`: последний зовёт
`~/call_tool` первого, активироваться раньше своего единственного клиента
незачем. `interaction_log` -- последним: ничего не гейтит и ничем не
гейтится (fire-and-forget подписчик `dialog_agent`, llm_plam.md §6), но
естественно идёт последним по порядку чтения потока событий.

Этот пакет пока не зарегистрирован в guide_robot_supervisor -- регистрация
там (по образцу mission/voice/semantic_map) откладывается до ручной
проверки живого стека -- отдельная задача после этого шага.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

BRINGUP_ORDER = ["tool_broker", "dialog_agent", "interaction_log"]


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
                [FindPackageShare("guide_robot_llm"), "config", "llm.yaml"]
            ),
        ),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    node_params = [ParameterFile(params, allow_substs=True), {"use_sim_time": use_sim_time}]

    tool_broker = Node(
        package="guide_robot_llm",
        executable="tool_broker",
        name="tool_broker",
        output="screen",
        parameters=node_params,
        arguments=["--ros-args", "--log-level", log_level],
    )

    dialog_agent = Node(
        package="guide_robot_llm",
        executable="dialog_agent",
        name="dialog_agent",
        output="screen",
        parameters=node_params,
        arguments=["--ros-args", "--log-level", log_level],
    )

    interaction_log = Node(
        package="guide_robot_llm",
        executable="interaction_log",
        name="interaction_log",
        output="screen",
        parameters=node_params,
        arguments=["--ros-args", "--log-level", log_level],
    )

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_llm",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "autostart": autostart,
                "node_names": BRINGUP_ORDER,
                # 0.0 -- обычные rclpy.lifecycle.LifecycleNode, bond не
                # создают (см. то же в mission.launch.py/voice.launch.py).
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, tool_broker, dialog_agent, interaction_log, manager])
