"""Запуск всего голосового стека.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию. Причина та
же, что и в safety-слое: микрофон и динамик гасятся на зарядке, и порядок
переходов должен быть управляемым извне, а не зашитым в launch. Подъём --
дело оркестратора тура (mission) либо ручных `ros2 lifecycle set`.

autostart:=true поднимает всё сразу через lifecycle_manager -- удобно для
разработки и для tts_only.launch.py, но не рабочий режим на роботе.

Порядок bring-up (design §3, критичен при autostart:=true):
  tts_node -> audio_frontend -> vad_node -> wakeword_node -> asr_node
TTS должен уметь сказать "инициализация" до того, как поднят вход;
микрофонная цепочка активируется последней, чтобы не ловить собственные
тестовые тоны.
"""

from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription

NODES = ["audio_frontend", "vad_node", "wakeword_node", "asr_node", "tts_node"]

# Порядок существенен -- см. шапку модуля. Отдельно от NODES (порядок
# инстанцирования Node-действий в launch не имеет значения, ROS pub/sub
# терпит поздних подписчиков/издателей; порядок lifecycle-переходов --
# то, что реально гасит "услышал собственный тестовый тон").
BRINGUP_ORDER = ["tts_node", "audio_frontend", "vad_node", "wakeword_node", "asr_node"]


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
            # allow_substs: voice.yaml содержит $(find-pkg-share guide_robot_voice)
            # в путях к моделям -- без этого флага ParameterFile передаёт
            # строку буквально, и модели не находятся ни на одной машине.
            parameters=[ParameterFile(params, allow_substs=True)],
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
                "node_names": BRINGUP_ORDER,
                # 0.0 -- явно выключить bond. lifecycle_manager по умолчанию
                # ждёт heartbeat через bond от управляемой ноды (так делают
                # C++-ноды Nav2 на nav2_util::LifecycleNode); наши ноды --
                # обычные rclpy.lifecycle.LifecycleNode, bond не создают.
                # Без этой строки ноды активируются корректно, но менеджер
                # всё равно валит bringup через bond_timeout секунд, ожидая
                # heartbeat, которого никогда не будет.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, GroupAction([*nodes, manager])])
