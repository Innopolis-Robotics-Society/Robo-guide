from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node


def generate_launch_description():
    """Launch the lifecycle supervisor with its group and watchdog config."""
    pkg = get_package_share_directory("guide_robot_supervisor")

    config_file = LaunchConfiguration("config_file")
    autostart_supervisor = LaunchConfiguration("autostart_supervisor")
    use_sim_time = LaunchConfiguration("use_sim_time")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "config_file",
                default_value=PathJoinSubstitution([pkg, "config", "supervisor.yaml"]),
            ),
            DeclareLaunchArgument("autostart_supervisor", default_value="true"),
            DeclareLaunchArgument("use_sim_time", default_value="false"),
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
                        "autostart": autostart_supervisor,
                        "use_sim_time": use_sim_time,
                        "loop_period": 0.2,
                        "estop_topic": "/supervisor/estop",
                    }
                ],
            ),
        ]
    )
