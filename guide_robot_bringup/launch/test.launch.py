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

    # 2. Sensors Launch (Lidars + Scan Merger)
    sensors_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(bringup_dir, "launch", "sensors.launch.py"))
    )

    # 3. Sonar Node
    sonar_node = Node(
        package="guide_robot_sonar", executable="sonar_node.py", name="sonar_node", output="screen"
    )

    # 4. Foxglove Bridge Node (для подключения извне к порту 8765)
    foxglove_bridge = Node(
        package="foxglove_bridge",
        executable="foxglove_bridge",
        name="foxglove_bridge",
        output="screen",
        parameters=[
            {
                "port": 8765,
                "address": "0.0.0.0",
                "send_buffer_limit": 100000000,
            }
        ],
    )

    return LaunchDescription([hardware_launch, sensors_launch, sonar_node, foxglove_bridge])
