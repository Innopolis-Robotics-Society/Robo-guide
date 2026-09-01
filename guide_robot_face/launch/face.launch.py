r"""Запуск face_node -- отдаёт web/, websocket на /face/state, /face/set_state.

Проверка (stage 1, §5):
  ros2 launch guide_robot_face face.launch.py
  # браузер: http://<jetson>:8090
  ros2 service call /face/set_state guide_robot_msgs/srv/SetFaceState \
      "{state: 'thinking', gaze_az: 0.0, seq: 1}"
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
                [FindPackageShare("guide_robot_face"), "config", "face_node.yaml"]
            ),
        ),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    face_node = Node(
        package="guide_robot_face",
        executable="face_node",
        name="face_node",
        output="screen",
        # allow_substs: face_node.yaml содержит $(find-pkg-share guide_robot_face)
        # в web_root/states_yaml -- без флага ParameterFile передаёт строку
        # буквально, не раскрывая подстановку.
        parameters=[ParameterFile(params, allow_substs=True)],
        arguments=["--ros-args", "--log-level", log_level],
    )

    return LaunchDescription([*arguments, face_node])
