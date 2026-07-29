import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    pkg = get_package_share_directory("guide_robot_navigation")
    nav2_launch_dir = os.path.join(
        get_package_share_directory("nav2_bringup"), "launch"
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart_nav = LaunchConfiguration("autostart_nav")
    map_yaml = LaunchConfiguration("map")
    nav2_params = LaunchConfiguration("nav2_params_file")

    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false",
        description="Use simulation clock",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav", default_value="false",
        description="Autostart lifecycle nodes",
    )
    declare_map = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg, "map", "simple.yaml"),
        description="Full path to map yaml",
    )
    declare_nav2_params = DeclareLaunchArgument(
        "nav2_params_file",
        default_value=os.path.join(pkg, "config", "first_iter_nav2.yaml"),
        description="Nav2 parameters file",
    )

    # map_server + amcl from nav2_bringup, non-composed to match common.
    localization = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_launch_dir, "localization_launch.py")
        ),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart": autostart_nav,
            "params_file": nav2_params,
            "map": map_yaml,
            "use_composition": "False",
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
        declare_autostart_nav,
        declare_map,
        declare_nav2_params,
        localization,
        common,
    ])