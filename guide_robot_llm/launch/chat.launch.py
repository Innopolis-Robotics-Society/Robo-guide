r"""Запуск `chat_node`.

Lifecycle-нода не поднимается автоматически по умолчанию -- порядок подъёма
принадлежит супервизору тура (design §2), как и у нод `guide_robot_voice`.
`autostart:=true` поднимает ноду сама через `lifecycle_manager`, удобно
для разработки и для ручной проверки петли `/asr/transcript -> LLM -> say`.

Отдельного launch, поднимающего voice+llm вместе, в Stage 0 нет:
`voice.launch.py` и `chat.launch.py` запускаются рядом (design §11).

Проверка (Stage 0, backend: echo, require_wakeword: false):
  ros2 lifecycle set /chat_node configure   # если autostart:=false
  ros2 lifecycle set /chat_node activate
  ros2 topic pub --once /asr/transcript guide_robot_msgs/msg/Transcript \\
      "{text: 'привет', is_final: true, confidence: 1.0}"
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    params = LaunchConfiguration("params_file")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_llm"), "config", "llm.yaml"]
            ),
        ),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    chat_node = Node(
        package="guide_robot_llm",
        executable="chat_node",
        name="chat_node",
        output="screen",
        # allow_substs: llm.yaml не содержит find-pkg-share сейчас, но
        # system_prompt_file может получить такой путь -- тот же флаг,
        # что и в guide_robot_voice, на будущее и для единообразия.
        parameters=[ParameterFile(params, allow_substs=True)],
        arguments=["--ros-args", "--log-level", log_level],
    )

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_chat",
        output="screen",
        condition=IfCondition(autostart),
        parameters=[
            {
                "autostart": True,
                "node_names": ["chat_node"],
                # 0.0 -- явно выключить bond, см. voice.launch.py: chat_node --
                # обычный rclpy.lifecycle.LifecycleNode, bond не создаёт.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, chat_node, manager])
