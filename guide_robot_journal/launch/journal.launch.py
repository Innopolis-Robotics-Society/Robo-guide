"""Opt-in process; all sources and retention limits live in the supplied YAML."""
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    default = str(Path(get_package_share_directory("guide_robot_journal")) / "config/journal.yaml")
    return LaunchDescription([
        DeclareLaunchArgument("journal_config", default_value=default),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        Node(package="guide_robot_journal", executable="robot_journal", output="screen",
             parameters=[{"config_file": LaunchConfiguration("journal_config"),
                          "use_sim_time": LaunchConfiguration("use_sim_time")}]),
    ])
