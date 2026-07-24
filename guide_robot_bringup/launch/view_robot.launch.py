#!/usr/bin/env python3
"""
Просмотр описания Guide robot в RViz.

Запускает robot_state_publisher (генерит TF из URDF),
joint_state_publisher_gui (ползунки для колёс) и RViz.
Используется для визуальной проверки геометрии и TF-дерева
БЕЗ реального робота и без ros2_control.

    ros2 launch guide_robot_description view_robot.launch.py
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import Command
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Собрать LaunchDescription для просмотра модели робота в RViz."""
    pkg_share_guide_robot_description = get_package_share_directory("guide_robot_description")
    pkg_share_guide_robot_bringup = get_package_share_directory("guide_robot_bringup")

    xacro_file = os.path.join(pkg_share_guide_robot_description, "urdf", "guide_robot.urdf.xacro")
    rviz_config = os.path.join(pkg_share_guide_robot_bringup, "rviz", "view_robot.rviz")

    # robot_description получаем прогоном xacro в runtime
    robot_description = {
        "robot_description": ParameterValue(
            Command(["xacro ", xacro_file, " use_mock_hardware:=true"]),
            value_type=str,
        )
    }

    gui_arg = DeclareLaunchArgument(
        "gui",
        default_value="true",
        description="Запустить joint_state_publisher_gui с ползунками колёс",
    )

    robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[robot_description],
    )

    joint_state_publisher_gui = Node(
        package="joint_state_publisher_gui",
        executable="joint_state_publisher_gui",
        condition=None,  # всегда для просмотра; в bringup заменяется broadcaster'ом
    )

    rviz = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        output="screen",
        arguments=["-d", rviz_config],
    )

    return LaunchDescription(
        [
            gui_arg,
            robot_state_publisher,
            joint_state_publisher_gui,
            rviz,
        ]
    )
