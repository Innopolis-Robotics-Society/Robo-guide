import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    """Launch hardware, lidars, sonars, and Foxglove Bridge for testing."""
    bringup_dir = get_package_share_directory("guide_robot_bringup")

    # 1. Hardware Launch (URDF, Robot State Publisher, Motor Driver)
    hardware_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(bringup_dir, "launch", "hardware.launch.py"))
    )


    return LaunchDescription([hardware_launch])
