"""Запуск голосового стека.

Ноды lifecycle и НЕ поднимаются автоматически. Причина та же, что
в safety-слое: микрофон и динамик гасятся на зарядке, и порядок переходов
должен быть управляемым извне, а не зашитым в launch. Подъём делает
lifecycle_manager_voice либо оркестратор тура.

autostart:=true поднимает всё сразу -- режим отладки, не рабочий.
"""

from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription

NODES = [
    "audio_frontend",
    "vad_node",
    "wakeword_node",
    "asr_node",
    "turn_detector",
    "tts_node",
]


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    params = LaunchConfiguration("params_file")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_voice"), "config", "voice.yaml"]
            ),
        ),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    nodes = [
        Node(
            package="guide_robot_voice",
            executable=name,
            name=name,
            output="screen",
            parameters=[params],
            arguments=["--ros-args", "--log-level", log_level],
        )
        for name in NODES
    ]

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_voice",
        output="screen",
        condition=IfCondition(autostart),
        parameters=[
            {
                "autostart": True,
                # Порядок существенен: сток должен быть готов раньше,
                # чем VAD получит право слать cancel_all.
                "node_names": [
                    "tts_node",
                    "audio_frontend",
                    "vad_node",
                    "wakeword_node",
                    "asr_node",
                    "turn_detector",
                ],
                "bond_timeout": 10.0,
            }
        ],
    )

    return LaunchDescription([*arguments, GroupAction([*nodes, manager])])
