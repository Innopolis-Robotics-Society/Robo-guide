# =========================================================================
#  simulation.launch.py — top-level simulation entry point.
#
#  Только склейка:
#    1. gazebo.launch.py            — Gazebo + робот + контроллеры
#    2. perception.launch.py        — мерджер /scan_left + /scan_right -> /scan
#    3. nav_stack.launch.py         — SLAM или AMCL + Nav2 + супервизор
#    4. high_level_stack.launch.py  — стек экскурсий: voice + semantic_map + mission_control
#    5. RViz
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
        default_value=os.path.join(pkg_navigation, "map", "innopark_l_10.09_edited.yaml"),
        description="Map yaml, used only when slam:=false",
    )
    declare_keepout_mask_file = DeclareLaunchArgument(
        "keepout_mask_file",
        default_value=os.path.join(pkg_navigation, "map", "innopark_l_10.09_edited_keepout.yaml"),
        description="Keepout costmap-filter mask, must match `map`. 'none' -- filter off.",
    )
    world_arg = DeclareLaunchArgument(
        name="world",
        default_value=os.path.join(pkg_simulation, "worlds", "innopark_edited.world"),
        description="Path to the Gazebo world file",
    )
    # Спавн на стоянке (parking в locations_innopark.yaml) -- оттуда робот
    # стартует, AMCL ждёт его там же (initial_pose в first_iter_nav2.yaml.in).
    spawn_x_arg = DeclareLaunchArgument(
        name="spawn_x",
        default_value="20.686",
        description="X (m) of base_footprint at spawn in Gazebo. Must match "
        "amcl.initial_pose.x in first_iter_nav2.yaml.in.",
    )
    spawn_y_arg = DeclareLaunchArgument(
        name="spawn_y",
        default_value="1.027",
        description="Y (m) of base_footprint at spawn in Gazebo. Must match "
        "amcl.initial_pose.y in first_iter_nav2.yaml.in.",
    )
    spawn_yaw_arg = DeclareLaunchArgument(
        name="spawn_yaw",
        default_value="-0.218",
        description="Yaw (rad) of base_footprint at spawn in Gazebo. Must match "
        "amcl.initial_pose.yaw in first_iter_nav2.yaml.in.",
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
    declare_launch_high_level = DeclareLaunchArgument(
        "launch_high_level",
        default_value="true",
        description="Launch the tour stack (voice + semantic_map + mission_control)",
    )

    slam = LaunchConfiguration("slam")
    map_yaml = LaunchConfiguration("map")
    keepout_mask_file = LaunchConfiguration("keepout_mask_file")
    world = LaunchConfiguration("world")
    spawn_x = LaunchConfiguration("spawn_x")
    spawn_y = LaunchConfiguration("spawn_y")
    spawn_yaw = LaunchConfiguration("spawn_yaw")
    nav = LaunchConfiguration("nav")
    nav_params = LaunchConfiguration("nav_params_file")
    slam_params = LaunchConfiguration("slam_params_file")
    use_rviz = LaunchConfiguration("rviz")
    launch_high_level = LaunchConfiguration("launch_high_level")

    # ── 1. Симуляция: Gazebo + робот ─────────────────────────────────────────
    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_simulation, "launch", "gazebo.launch.py")),
        launch_arguments={
            "use_sim_time": "true",
            "world": world,
            "spawn_x": spawn_x,
            "spawn_y": spawn_y,
            "spawn_yaw": spawn_yaw,
        }.items(),
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
            "keepout_mask_file": keepout_mask_file,
            "autostart_nav": "false",
            "launch_supervisor": "true",
            "autostart_supervisor": "true",
        }.items(),
    )

    # ── 4. Стек экскурсий ────────────────────────────────────────────────────
    high_level_stack = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_bringup, "launch", "high_level_stack.launch.py")
        ),
        condition=IfCondition(launch_high_level),
        launch_arguments={"use_sim_time": "true"}.items(),
    )

    # ── 5. RViz ──────────────────────────────────────────────────────────────
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
            declare_keepout_mask_file,
            declare_nav,
            world_arg,
            spawn_x_arg,
            spawn_y_arg,
            spawn_yaw_arg,
            declare_nav_params,
            declare_slam_params,
            declare_rviz,
            declare_launch_high_level,
            gazebo,
            perception,
            nav_stack,
            high_level_stack,
            rviz,
        ]
    )
