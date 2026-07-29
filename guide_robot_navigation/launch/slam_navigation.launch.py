import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    pkg = get_package_share_directory("guide_robot_navigation")
    slam_launch_dir = os.path.join(
        get_package_share_directory("slam_toolbox"), "launch"
    )

    autostart_nav = LaunchConfiguration("autostart_nav")
    nav2_params = LaunchConfiguration("nav2_params_file")

    use_sim_time = LaunchConfiguration("use_sim_time")
    slam_params = LaunchConfiguration("slam_params_file")

    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false",
        description="Use simulation clock",
    )
    declare_slam_params = DeclareLaunchArgument(
        "slam_params_file",
        default_value=os.path.join(
            pkg, "config", "mapper_params_online_async.yaml"
        ),
        description="slam_toolbox parameters file",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav", default_value="false",
        description="Autostart lifecycle nodes",
    )
    declare_nav2_params = DeclareLaunchArgument(
        "nav2_params_file",
        default_value=os.path.join(pkg, "config", "first_iter_nav2.yaml"),
        description="Nav2 parameters file",
    )

    slam = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(slam_launch_dir, "online_async_launch.py")
        ),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "slam_params_file": slam_params,
        }.items(),
    )

    common = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg, "launch", "common.launch.py")
        ),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart": autostart_nav,
            "nav2_params_file": nav2_params,
        }.items(),
    )

    return LaunchDescription([
        declare_use_sim_time,
        declare_slam_params,
        declare_autostart_nav,
        declare_nav2_params,
        slam,
        common,
    ])