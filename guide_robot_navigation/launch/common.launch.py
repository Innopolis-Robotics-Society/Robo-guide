import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg = get_package_share_directory("guide_robot_navigation")
    nav2_launch_dir = os.path.join(
        get_package_share_directory("nav2_bringup"), "launch"
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart_nav = LaunchConfiguration("autostart_nav")
    nav2_params = LaunchConfiguration("nav2_params_file")

    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false",
        description="Use simulation clock",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav", default_value="false",
        description="Autostart the nav2 lifecycle nodes",
    )
    declare_nav2_params = DeclareLaunchArgument(
        "nav2_params_file",
        default_value=os.path.join(pkg, "config", "first_iter_nav2.yaml"),
        description="Nav2 parameters file",
    )

    # Nav2 core. composition off so respawn/remaps are predictable on real HW.
    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_launch_dir, "navigation_launch.py")
        ),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart": autostart_nav,
            "params_file": nav2_params,
            "use_composition": "False",
        }.items(),
    )

    # collision_monitor sits between velocity_smoother and the base.
    # cmd_vel_out is remapped to the diff_drive_controller unstamped input.
    collision_monitor = Node(
        package="nav2_collision_monitor",
        executable="collision_monitor",
        name="collision_monitor",
        output="screen",
        parameters=[nav2_params, 
                    {"use_sim_time": use_sim_time}
                ],
    )

    # Separate lifecycle manager so the safety node is managed independently
    # of the nav stack and survives a nav restart.
    lifecycle_safety = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_safety",
        output="screen",
        parameters=[{
            "use_sim_time": use_sim_time,
            "autostart": autostart_nav,
            "node_names": ["collision_monitor"],
            "bond_timeout": 4.0,
        }],
    )

    return LaunchDescription([
        declare_use_sim_time,
        declare_autostart_nav,
        declare_nav2_params,
        nav2,
        collision_monitor,
        lifecycle_safety,
    ])