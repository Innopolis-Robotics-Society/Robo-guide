import os
import tempfile

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node

from guide_robot_supervisor.config_tools import write_config_without_scan_topic


def _supervisor_node(context):
    config_file = LaunchConfiguration("config_file").perform(context)
    if LaunchConfiguration("right_lidar").perform(context).lower() in ("false", "0", "no"):
        # Один лидар: без /scan_right в проверке scan_rate (предусловие группы safety).
        config_file = write_config_without_scan_topic(
            config_file,
            os.path.join(
                tempfile.gettempdir(), f"supervisor_one_lidar_{os.path.basename(config_file)}"
            ),
            "/scan_right",
        )
    return [
        Node(
            package="guide_robot_supervisor",
            executable="supervisor",
            name="supervisor",
            output="screen",
            emulate_tty=True,
            parameters=[
                {
                    "config_file": config_file,
                    # launch arg is autostart_supervisor to keep it apart from
                    # autostart_nav one level up; the node's own param is autostart.
                    "autostart": LaunchConfiguration("autostart_supervisor"),
                    "use_sim_time": LaunchConfiguration("use_sim_time"),
                    "loop_period": 0.2,
                    "estop_topic": "/supervisor/estop",
                }
            ],
        )
    ]


def generate_launch_description():
    """Launch the lifecycle supervisor with its group and watchdog config."""
    pkg = get_package_share_directory("guide_robot_supervisor")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "config_file",
                default_value=PathJoinSubstitution([pkg, "config", "supervisor.yaml"]),
            ),
            DeclareLaunchArgument("autostart_supervisor", default_value="true"),
            DeclareLaunchArgument("use_sim_time", default_value="false"),
            DeclareLaunchArgument(
                "right_lidar",
                default_value="true",
                description="false -- один (левый) лидар: scan_rate не ждёт /scan_right",
            ),
            OpaqueFunction(function=_supervisor_node),
        ]
    )
