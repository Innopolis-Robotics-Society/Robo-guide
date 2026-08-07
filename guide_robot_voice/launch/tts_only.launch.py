r"""Отладочный запуск одного tts_node.

Только звук на выход: чанкер, планировщик, epoch-fencing, устройство
воспроизведения. Ни микрофона, ни VAD, ни ASR -- нода lifecycle и не
поднимается сама по себе, поэтому здесь она поднимается автостартом
по умолчанию (в отличие от voice.launch.py, где подъём -- дело
оркестратора тура).

Проверка (design §6, шаг 2):
  ros2 lifecycle set /tts_node configure   # если autostart:=false
  ros2 lifecycle set /tts_node activate
  ros2 action send_goal /say guide_robot_msgs/action/Say \\
      "{text: 'Проверка связи', priority: 50, scope: 1, interruptible: true}"
"""

from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription


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
        DeclareLaunchArgument("autostart", default_value="true"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    tts_node = Node(
        package="guide_robot_voice",
        executable="tts_node",
        name="tts_node",
        output="screen",
        # allow_substs: voice.yaml содержит $(find-pkg-share guide_robot_voice)
        # в model_path -- без этого флага ParameterFile передаёт строку буквально,
        # и Piper получает путь, который не существует ни на одной машине.
        parameters=[ParameterFile(params, allow_substs=True)],
        arguments=["--ros-args", "--log-level", log_level],
    )

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_tts_only",
        output="screen",
        condition=IfCondition(autostart),
        parameters=[
            {
                "autostart": True,
                "node_names": ["tts_node"],
                # 0.0 -- явно выключить bond. lifecycle_manager по умолчанию
                # ждёт heartbeat через bond от управляемой ноды (так делают
                # C++-ноды Nav2 на nav2_util::LifecycleNode); наш tts_node --
                # обычный rclpy.lifecycle.LifecycleNode, bond не создаёт.
                # Без этой строки нода активируется корректно, но менеджер
                # всё равно валит bringup через bond_timeout секунд, ожидая
                # heartbeat, которого никогда не будет.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, tts_node, manager])
