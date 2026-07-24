# =========================================================================
#  simulation.launch.py - top-level simulation entry point.
#
#  Brings up, in order:
#    1. gazebo.launch.py        - Gazebo + robot + RViz (existing file)
#    2. controller spawners     - joint_state_broadcaster, diff_drive
#    3. cmd_vel relay           - /cmd_vel -> /diff_drive_controller/cmd_vel
#    4. laserscan merger        - /scan_left + /scan_right -> /scan
#    5. SLAM  (slam:=true)  OR  AMCL + saved map (slam:=false)
#    6. Nav2 stack              - planner, controller, bt_navigator, ...
#
#  Usage:
#    # mapping a new area
#    ros2 launch guide_robot_bringup simulation.launch.py slam:=true
#
#    # navigating a known map
#    ros2 launch guide_robot_bringup simulation.launch.py \
#        slam:=false map:=/abs/path/to/my_map.yaml
#
#    # simulation only, no navigation
#    ros2 launch guide_robot_bringup simulation.launch.py nav:=false
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch entire Simulator stack."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_navigation = get_package_share_directory("guide_robot_navigation")
    pkg_simulation = get_package_share_directory("guide_robot_simulation")

    # ------------------------------------------------------------------
    #  Launch arguments
    # ------------------------------------------------------------------
    slam_arg = DeclareLaunchArgument(
        "slam",
        default_value="False",
        description="True  -> SLAM Toolbox builds a map online. "
        "False -> AMCL localizes against the map given by map:=",
    )

    map_arg = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg_navigation, "map", "simple.yaml"),
        description="Map yaml, used only when slam:=false",
    )

    rviz_arg = DeclareLaunchArgument("rviz", default_value="true", description="Start RViz")

    nav_params_arg = DeclareLaunchArgument(
        "nav_params",
        default_value=os.path.join(pkg_navigation, "params", "first_iter_nav2.yaml"),
        description="Nav2 parameter file",
    )

    slam_params_arg = DeclareLaunchArgument(
        "slam_params",
        default_value=os.path.join(pkg_navigation, "params", "mapper_params_online_async.yaml"),
        description="SLAM Toolbox parameter file",
    )

    slam = LaunchConfiguration("slam")
    map_yaml = LaunchConfiguration("map")
    use_rviz = LaunchConfiguration("rviz")
    nav_params = LaunchConfiguration("nav_params")
    slam_params = LaunchConfiguration("slam_params")

    # ------------------------------------------------------------------
    #  1. Simulation: Gazebo + robot + RViz
    #     Reuses the existing gazebo.launch.py as-is.
    # ------------------------------------------------------------------
    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_simulation, "launch", "gazebo.launch.py")),
        launch_arguments={"use_sim_time": "true"}.items(),
    )

    # ------------------------------------------------------------------
    #  4. Laser scan merger: /scan_left + /scan_right -> /scan
    # mich1342/ros2_laser_scan_merger outputs PointCloud2.
    # NOTE: This specific merger is not fully TF-aware for input poses,
    # it uses manual offset parameters. We set them to match the URDF:
    # Left: Y = -0.258, Right: Y = +0.258
    # ------------------------------------------------------------------
    merger = Node(
        package="dual_laser_merger",
        executable="dual_laser_merger_node",
        name="dual_laser_merger",
        output="screen",
        remappings=[
            ("merged", "/scan"),
            ("merged_cloud", "/scan_merged_cloud"),
        ],
        parameters=[
            {"use_sim_time": True},
            {
                "laser_1_topic": "/scan_left",
                "laser_2_topic": "/scan_right",
                "target_frame": "base_footprint",
                "tolerance": 0.05,
                "queue_size": 10,
                "angle_increment": 0.005,
                "scan_time": 0.1,
                "range_min": 0.1,
                "range_max": 12.0,
                "min_height": -0.5,
                "max_height": 1.5,
                "angle_min": -3.141592654,
                "angle_max": 3.141592654,
                "use_inf": True,
                "inf_epsilon": 1.0,
                "enable_calibration": False,
                "enable_average_filter": False,
                "enable_shadow_filter": False,
            },
        ],
    )

    # ------------------------------------------------------------------
    #  5a. SLAM Toolbox (slam:=true) - builds the map online
    # ------------------------------------------------------------------
    slam_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_navigation, "launch", "slam_navigation.launch.py")
        ),
        condition=IfCondition(slam),
        launch_arguments={
            "use_sim_time": "true",
            "slam_params_file": slam_params,
            "nav2_params_file": nav_params,
        }.items(),
    )

    # ------------------------------------------------------------------
    #  6. Nav2 stack (planner / controller / behaviors / BT navigator)
    #     navigation_launch.py brings up everything except localization,
    #     which is handled above by either SLAM or AMCL.
    # ------------------------------------------------------------------

    nav2 = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_navigation, "launch", "navigation.launch.py")
        ),
        condition=UnlessCondition(slam),
        launch_arguments={
            "use_sim_time": "true",
            "params_file": nav_params,
            "map": map_yaml,
        }.items(),
    )

    # Nav2 needs a TF tree and a /scan before it starts planning.
    delayed_nav2 = TimerAction(period=10.0, actions=[nav2])
    delayed_slam = TimerAction(period=10.0, actions=[slam_launch])

    # ------------------------------------------------------------------
    #  RViz with the simulation config
    # ------------------------------------------------------------------
    rviz = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        condition=IfCondition(use_rviz),
        arguments=["-d", os.path.join(pkg_bringup, "rviz", "sim.rviz")],
        parameters=[{"use_sim_time": True}],
        output="screen",
    )

    return LaunchDescription(
        [
            # arguments
            slam_arg,
            map_arg,
            rviz_arg,
            nav_params_arg,
            slam_params_arg,
            # simulation
            gazebo,
            # perception
            merger,
            # localization
            delayed_slam,
            # navigation
            delayed_nav2,
            # visualization
            rviz,
        ]
    )
