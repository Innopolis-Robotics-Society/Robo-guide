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

    # ------------------------------------------------------------------
    #  Launch arguments
    # ------------------------------------------------------------------
    slam_arg = DeclareLaunchArgument(
        "slam",
        default_value="true",
        description="true  -> SLAM Toolbox builds a map online. "
        "false -> AMCL localizes against the map given by map:=",
    )

    map_arg = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg_navigation, "map", "my_map.yaml"),
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
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "gazebo.launch.py")),
        launch_arguments={"use_sim_time": "true"}.items(),
    )

    # ------------------------------------------------------------------
    #  4. Laser scan merger: /scan_left + /scan_right -> /scan
    # mich1342/ros2_laser_scan_merger outputs PointCloud2.
    # NOTE: This specific merger is not fully TF-aware for input poses,
    # it uses manual offset parameters. We set them to match the URDF:
    # Left: Y = -0.258, Right: Y = +0.258
    # ------------------------------------------------------------------
    scan_merger = Node(
        package="ros2_laser_scan_merger",
        executable="ros2_laser_scan_merger",
        name="scan_merger",
        output="screen",
        parameters=[
            {
                "use_sim_time": True,
                "scanTopic1": "/scan_left",
                "laser1XOff": 0.0,
                "laser1YOff": -0.258,
                "laser1ZOff": 0.0,
                "laser1Alpha": 0.0,
                "show1": True,
                "scanTopic2": "/scan_right",
                "laser2XOff": 0.0,
                "laser2YOff": 0.258,
                "laser2ZOff": 0.0,
                "laser2Alpha": 0.0,
                "show2": True,
                "pointCloudTopic": "/scan_merged_cloud",
                "pointCloutFrameId": "base_footprint",
            }
        ],
    )

    # Convert the merged PointCloud2 back into a unified LaserScan on /scan
    pc_to_laserscan = Node(
        package="pointcloud_to_laserscan",
        executable="pointcloud_to_laserscan_node",
        name="pc_to_laserscan",
        output="screen",
        remappings=[
            ("cloud_in", "/scan_merged_cloud"),
            ("scan", "/scan"),
        ],
        parameters=[
            {
                "use_sim_time": True,
                "target_frame": "base_footprint",
                "transform_tolerance": 0.01,
                "min_height": -0.5,
                "max_height": 1.5,
                "angle_min": -3.14159,
                "angle_max": 3.14159,
                "angle_increment": 0.005,
                "scan_time": 0.1,
                "range_min": 0.1,
                "range_max": 12.0,
                "use_inf": True,
                "inf_epsilon": 1.0,
            }
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
            scan_merger,
            pc_to_laserscan,
            # localization
            delayed_slam,
            # navigation
            delayed_nav2,
            # visualization
            rviz,
        ]
    )
