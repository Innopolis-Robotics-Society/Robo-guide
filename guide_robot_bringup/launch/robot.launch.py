r"""Полный стек одной кнопкой: hardware.launch.py + operator_ui.

Это то, что запускает start_stack.sh (guide-launcher). Ни один аргумент здесь
не переобъявляется: IncludeLaunchDescription выполняется в общем LaunchContext,
поэтому DeclareLaunchArgument внутри hardware.launch.py и operator_ui.launch.py
сам подхватит значение, уже выставленное `ros2 launch ... key:=value`, а если
его нет -- останется на своём дефолте, как при прямом запуске hardware.launch.py.

  ros2 launch guide_robot_bringup robot.launch.py
  ros2 launch guide_robot_bringup robot.launch.py slam:=true launch_rviz:=true
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    """hardware.launch.py (шасси, навигация, стек экскурсий) + operator_ui (мост :8091)."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_operator_ui = get_package_share_directory("guide_robot_operator_ui")

    hardware = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "hardware.launch.py"))
    )
    operator_ui = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_operator_ui, "launch", "operator_ui.launch.py")
        )
    )

    return LaunchDescription([hardware, operator_ui])
