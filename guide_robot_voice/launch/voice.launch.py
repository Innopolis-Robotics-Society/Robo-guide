"""Запуск всего голосового стека.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию. Причина та
же, что и в safety-слое: микрофон и динамик гасятся на зарядке, и порядок
переходов должен быть управляемым извне, а не зашитым в launch.

Зарегистрирован в guide_robot_supervisor как группа `voice`
(config/supervisor.yaml, config/supervisor_slam.yaml, optional: true) --
тем же способом, что и `mission` (см. guide_robot_mission_control/launch/
mission.launch.py). `lifecycle_manager_voice` запускается ВСЕГДА (не под
условием) -- супервизор дёргает его `~/manage_nodes` напрямую, сервис
обязан существовать независимо от того, кто инициирует STARTUP; см.
`lifecycle_manager_safety` в guide_robot_navigation/launch/common.launch.py,
тот же паттерн. Параметр `autostart` пробрасывается в сам lifecycle_manager
как есть (default "false" -- контракт супервизора: "Every lifecycle_manager
referenced here MUST have autostart: false"); autostart:=true остаётся для
разработки и для tts_only.launch.py -- самостоятельный подъём без
супервизора.

Порядок bring-up (design §3, критичен при autostart:=true):
  tts_node -> audio_frontend -> vad_node -> wakeword_node -> asr_node
TTS должен уметь сказать "инициализация" до того, как поднят вход;
микрофонная цепочка активируется последней, чтобы не ловить собственные
тестовые тоны.
"""

from launch.actions import DeclareLaunchArgument, GroupAction
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
        # БЕЗ condition=IfCondition(autostart): супервизор дёргает
        # ~/manage_nodes этого менеджера напрямую (guide_robot_supervisor/
        # config/supervisor.yaml, группа "voice"), сервис обязан
        # существовать вне зависимости от того, кто инициирует STARTUP.
        parameters=[
            {
                # Пробрасываем launch-arg как есть (default "false") --
                # контракт супервизора: "Every lifecycle_manager referenced
                # here MUST have autostart: false".
                "autostart": autostart,
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
