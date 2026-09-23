"""
Стендовый запуск ALSA-владельца XVF3800.

autostart=true предназначен для проверки на компьютере. В составе робота
переходами lifecycle будет управлять общий supervisor.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import LifecycleNode, Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    """Собрать стендовый launch с lifecycle autostart."""
    params = LaunchConfiguration("params_file")
    autostart = LaunchConfiguration("autostart")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "params_file",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("guide_robot_audio"), "config", "xvf3800.yaml"]
                ),
            ),
            DeclareLaunchArgument("autostart", default_value="true"),
            LifecycleNode(
                package="guide_robot_audio",
                executable="xvf3800_audio_node",
                name="xvf3800_audio_node",
                namespace="",
                output="screen",
                parameters=[ParameterFile(params, allow_substs=True)],
            ),
            Node(
                package="nav2_lifecycle_manager",
                executable="lifecycle_manager",
                name="lifecycle_manager_xvf3800_audio",
                output="screen",
                parameters=[
                    {
                        "autostart": autostart,
                        "node_names": ["xvf3800_audio_node"],
                        "bond_timeout": 0.0,
                    }
                ],
            ),
        ]
    )
