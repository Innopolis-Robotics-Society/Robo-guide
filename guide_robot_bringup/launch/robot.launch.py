r"""Полный стек одной кнопкой: hardware.launch.py + robot_api.

Это то, что запускает start_stack.sh (guide-launcher). Аргументы вложенных launch
не переобъявляются: IncludeLaunchDescription выполняется в общем LaunchContext,
поэтому DeclareLaunchArgument внутри hardware.launch.py и api.launch.py
сам подхватит значение, уже выставленное `ros2 launch ... key:=value`, а если
его нет -- останется на своём дефолте, как при прямом запуске hardware.launch.py.

  ros2 launch guide_robot_bringup robot.launch.py
  ros2 launch guide_robot_bringup robot.launch.py slam:=true launch_rviz:=true
  ros2 launch guide_robot_bringup robot.launch.py launch_journal:=true journal_config:=/path/journal.yaml
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    """hardware.launch.py (шасси, навигация, стек экскурсий) + robot_api (мост :8091)."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_robot_api = get_package_share_directory("guide_robot_api")

    hardware = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "hardware.launch.py"))
    )
    robot_api = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_robot_api, "launch", "api.launch.py")
        )
    )

    journal = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution([
            FindPackageShare("guide_robot_journal"), "launch", "journal.launch.py"])),
        condition=IfCondition(LaunchConfiguration("launch_journal")),
    )
    return LaunchDescription([
        DeclareLaunchArgument("launch_journal", default_value="false"),
        hardware, robot_api, journal,
    ])
