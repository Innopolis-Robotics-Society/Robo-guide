import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """Launch Nav2 + SLAM stack."""
    use_sim_time = LaunchConfiguration("use_sim_time", default="false")
    map_dir = LaunchConfiguration(
        "map",
        default=os.path.join(
            get_package_share_directory("guide_robot_navigation"), "map", "map.yaml"
        ),
    )

    nav2_params_file = LaunchConfiguration(
        "nav2_params_file",
        default=os.path.join(
            get_package_share_directory("guide_robot_navigation"), "params", "first_iter_nav2.yaml"
        ),
    )

    slam_params_file = LaunchConfiguration(
        "slam_params_file",
        default=os.path.join(
            get_package_share_directory("guide_robot_navigation"), "params", "first_iter_nav2.yaml"
        ),
    )

    nav2_launch_file_dir = os.path.join(get_package_share_directory("nav2_bringup"), "launch")

    slam_launch_file_dir = os.path.join(get_package_share_directory("slam_toolbox"), "launch")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "map", default_value=map_dir, description="Full path to map file to load"
            ),
            DeclareLaunchArgument(
                "nav2_params_file",
                default_value=nav2_params_file,
                description="Full path to param file to load",
            ),
            DeclareLaunchArgument(
                "slam_params_file",
                default_value=nav2_params_file,
                description="Full path to param file to load",
            ),
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
                description="Use simulation (Gazebo) clock if true",
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource([slam_launch_file_dir, "/online_async_launch.py"]),
                launch_arguments={
                    "use_sim_time": "True",
                    "slam_params_file": slam_params_file,
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource([nav2_launch_file_dir, "/navigation_launch.py"]),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "params_file": nav2_params_file,
                }.items(),
            ),
        ]
    )
