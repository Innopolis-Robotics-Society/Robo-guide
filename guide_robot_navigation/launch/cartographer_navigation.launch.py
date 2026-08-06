import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch Cartographer, its occupancy grid, and optionally Nav2."""
    pkg = get_package_share_directory("guide_robot_navigation")

    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart_nav = LaunchConfiguration("autostart_nav")
    nav2_params = LaunchConfiguration("nav2_params_file")
    nav = LaunchConfiguration("nav")

    # dual_laser_merger publishes BEST_EFFORT, but Cartographer subscribes
    # RELIABLE.  Relay to a dedicated reliable topic; Nav2 keeps using /scan.
    scan_qos_relay = Node(
        package="guide_robot_navigation",
        executable="scan_qos_relay",
        name="scan_qos_relay",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "input_topic": "/scan",
                "output_topic": "/scan_cartographer",
            }
        ],
    )

    cartographer = Node(
        package="cartographer_ros",
        executable="cartographer_node",
        name="cartographer_node",
        output="screen",
        parameters=[{"use_sim_time": use_sim_time}],
        arguments=[
            "-configuration_directory",
            os.path.join(pkg, "config"),
            "-configuration_basename",
            "guide_robot_2d.lua",
        ],
        remappings=[
            ("scan", "/scan_cartographer"),
            # Gazebo exposes the IMU on /imu/data, while Cartographer
            # subscribes to the relative topic "imu".  A hardware driver
            # must provide the same canonical topic when this config is used.
            # Without this remap its ordered sensor queue waits forever and
            # no submap or map -> odom transform is ever produced.
            ("imu", "/imu/data"),
            ("odom", "/odom"),
        ],
    )

    occupancy_grid = Node(
        package="cartographer_ros",
        executable="cartographer_occupancy_grid_node",
        name="cartographer_occupancy_grid_node",
        output="screen",
        parameters=[{"use_sim_time": use_sim_time}],
        arguments=[
            "-resolution", "0.05",
            "-publish_period_sec", "1.0",
        ],
    )

    # SubmapsDisplay alpha-blends unknown texture cells, so the real rectangular
    # extent of every stored submap is otherwise invisible.  This companion
    # layer queries texture metadata and publishes explicit bounds + dimensions.
    submap_boundaries = Node(
        package="guide_robot_navigation",
        executable="submap_boundary_visualizer",
        name="submap_boundary_visualizer",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "submap_list_topic": "/submap_list",
                "submap_query_service": "/submap_query",
                "marker_topic": "/submap_boundaries",
            }
        ],
    )

    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg, "launch", "common.launch.py")
        ),
        condition=IfCondition(nav),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart_nav": autostart_nav,
            "nav2_params_file": nav2_params,
        }.items(),
    )

    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        DeclareLaunchArgument("autostart_nav", default_value="true"),
        DeclareLaunchArgument(
            "nav2_params_file",
            default_value=os.path.join(
                pkg, "config", "first_iter_nav2.yaml"
            ),
        ),
        DeclareLaunchArgument(
            "nav",
            default_value="true",
            description="Launch Nav2 together with Cartographer",
        ),
        scan_qos_relay,
        cartographer,
        occupancy_grid,
        submap_boundaries,
        nav2,
    ])
