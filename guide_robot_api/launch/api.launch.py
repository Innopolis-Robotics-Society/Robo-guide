r"""Запуск приватного ROS API-моста, без браузера.

Проверка:
  ros2 launch guide_robot_api api.launch.py
  # liveness: http://127.0.0.1:8091/healthz
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    params = LaunchConfiguration("params_file")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_api"), "config", "api.yaml"]
            ),
        ),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    api_node = Node(
        package="guide_robot_api",
        executable="api_node",
        name="robot_api",
        output="screen",
        # allow_substs: api.yaml не подставляет find-pkg-share (все
        # дефолты вычисляются самой нодой из get_package_share_directory),
        # но держим тот же флаг, что и face.launch.py -- дешёвая
        # консистентность на случай, если параметр когда-то такое понадобится.
        parameters=[ParameterFile(params, allow_substs=True)],
        arguments=["--ros-args", "--log-level", log_level],
    )

    return LaunchDescription([*arguments, api_node])
