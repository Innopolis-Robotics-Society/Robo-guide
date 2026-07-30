# =========================================================================
#  simulation.launch.py — top-level simulation entry point.
#
#  Только склейка:
#    1. gazebo.launch.py     — Gazebo + робот + контроллеры
#    2. perception.launch.py — мерджер /scan_left + /scan_right -> /scan
#    3. nav_stack.launch.py  — SLAM или AMCL + Nav2 + супервизор
#    4. RViz
#
#  Usage:
#    ros2 launch guide_robot_bringup simulation.launch.py slam:=true
#    ros2 launch guide_robot_bringup simulation.launch.py \
#        slam:=false map:=/abs/path/to/my_map.yaml
#    ros2 launch guide_robot_bringup simulation.launch.py nav:=false
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch entire simulator stack."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_navigation = get_package_share_directory("guide_robot_navigation")
    pkg_simulation = get_package_share_directory("guide_robot_simulation")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_slam = DeclareLaunchArgument(
        "slam",
        default_value="false",
        description="true — SLAM Toolbox строит карту онлайн; false — AMCL по карте из map",
    )
    declare_map = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg_navigation, "map", "simple.yaml"),
        description="Map yaml, used only when slam:=false",
    )
    declare_nav = DeclareLaunchArgument(
        "nav", default_value="true", description="Launch Nav2 stack"
    )
    declare_nav_params = DeclareLaunchArgument(
        "nav_params_file",
        default_value=os.path.join(pkg_navigation, "config", "first_iter_nav2.yaml"),
        description="Nav2 parameter file",
    )
    declare_slam_params = DeclareLaunchArgument(
        "slam_params_file",
        default_value=os.path.join(pkg_navigation, "config", "mapper_params_online_async.yaml"),
        description="SLAM Toolbox parameter file",
    )
    declare_rviz = DeclareLaunchArgument("rviz", default_value="true", description="Start RViz")

    slam = LaunchConfiguration("slam")
    map_yaml = LaunchConfiguration("map")
    nav = LaunchConfiguration("nav")
    nav_params = LaunchConfiguration("nav_params_file")
    slam_params = LaunchConfiguration("slam_params_file")
    use_rviz = LaunchConfiguration("rviz")

    # ── 1. Симуляция: Gazebo + робот ─────────────────────────────────────────
    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_simulation, "launch", "gazebo.launch.py")),
        launch_arguments={"use_sim_time": "true"}.items(),
    )

    # ── 2. Перцепция: лидары виртуальные, соноры из плагинов Gazebo ──────────
    perception = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "perception.launch.py")),
        launch_arguments={
            "use_sim_time": "true",
            "real_lidars": "false",
            "launch_sonar": "false",
            "merge_frame": "base_footprint",
        }.items(),
    )

    # ── 3. Навигация ─────────────────────────────────────────────────────────
    nav_stack = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "nav_stack.launch.py")),
        launch_arguments={
            "use_sim_time": "true",
            "nav": nav,
            "slam": slam,
            "map": map_yaml,
            "nav_params_file": nav_params,
            "slam_params_file": slam_params,
            "autostart_nav": "false",
            "launch_supervisor": "true",
            "autostart_supervisor": "true",
        }.items(),
    )

    # ── 4. RViz ──────────────────────────────────────────────────────────────
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
            declare_slam,
            declare_map,
            declare_nav,
            declare_nav_params,
            declare_slam_params,
            declare_rviz,
            gazebo,
            perception,
            nav_stack,
            rviz,
        ]
    )