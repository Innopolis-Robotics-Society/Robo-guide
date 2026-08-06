# =========================================================================
#  hardware.launch.py — top-level entry point для реального робота.
#
#  Остаётся здесь только «базовое шасси»:
#    robot_state_publisher, ros2_control_node, спавнеры контроллеров,
#    Foxglove Bridge, RViz.
#
#  Всё остальное вынесено:
#    perception.launch.py       — лидары, бланкеры, мерджер, соноры
#    nav_stack.launch.py        — SLAM/AMCL + Nav2 + collision_monitor + супервизор
#    high_level_stack.launch.py — стек экскурсий: voice + semantic_map + mission_control
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    Command,
    FindExecutable,
    LaunchConfiguration,
    PathJoinSubstitution,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Generate the launch description for the Guide Robot hardware stack."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_navigation = get_package_share_directory("guide_robot_navigation")
    pkg_description = get_package_share_directory("guide_robot_description")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_mock = DeclareLaunchArgument(
        "use_mock_hardware", default_value="false", description="Launch robot without hardware"
    )
    # perception
    declare_launch_sensors = DeclareLaunchArgument(
        "launch_sensors", default_value="true", description="Launch lidars, merger and sonars"
    )
    declare_launch_sonar = DeclareLaunchArgument(
        "launch_sonar", default_value="true", description="Launch sonar range node"
    )
    # navigation
    declare_nav = DeclareLaunchArgument(
        "nav", default_value="true", description="Launch Nav2 stack"
    )
    declare_slam = DeclareLaunchArgument(
        "slam",
        default_value="false",
        description="true — строить карту SLAM Toolbox; false — AMCL по готовой карте из map",
    )
    declare_map = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg_navigation, "map", "lab_map.yaml"),
        description="Готовая карта для режима slam:=false (map_server + AMCL)",
    )
    declare_nav_params = DeclareLaunchArgument(
        "nav_params_file",
        default_value=os.path.join(pkg_navigation, "config", "first_iter_nav2.yaml"),
        description="Full path to Nav2 parameters file",
    )
    declare_slam_params = DeclareLaunchArgument(
        "slam_params_file",
        default_value=os.path.join(pkg_navigation, "config", "mapper_params_online_async.yaml"),
        description="Full path to SLAM Toolbox parameters file",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav", default_value="false", description="Autostart Nav2 lifecycle nodes"
    )
    declare_autostart_supervisor = DeclareLaunchArgument(
        "autostart_supervisor",
        default_value="true",
        description="Let the supervisor bring the lifecycle groups up on its own; "
        "false keeps it idle in INIT until /supervisor/bringup is called",
    )
    # high-level stack (tours)
    declare_launch_high_level = DeclareLaunchArgument(
        "launch_high_level",
        default_value="true",
        description="Launch the tour stack (voice + semantic_map + mission_control)",
    )
    # tooling
    declare_launch_foxglove = DeclareLaunchArgument(
        "launch_foxglove", default_value="true", description="Launch Foxglove Bridge"
    )
    declare_launch_rviz = DeclareLaunchArgument(
        "launch_rviz",
        default_value="true",
        description="Launch RViz (requires a display; keep off on the headless robot)",
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    use_mock_hardware = LaunchConfiguration("use_mock_hardware")
    launch_sensors = LaunchConfiguration("launch_sensors")
    launch_sonar = LaunchConfiguration("launch_sonar")
    nav = LaunchConfiguration("nav")
    slam = LaunchConfiguration("slam")
    map_yaml_file = LaunchConfiguration("map")
    nav_params_file = LaunchConfiguration("nav_params_file")
    slam_params_file = LaunchConfiguration("slam_params_file")
    autostart_nav = LaunchConfiguration("autostart_nav")
    autostart_supervisor = LaunchConfiguration("autostart_supervisor")
    launch_high_level = LaunchConfiguration("launch_high_level")
    launch_foxglove = LaunchConfiguration("launch_foxglove")
    launch_rviz = LaunchConfiguration("launch_rviz")

    # ── Robot description & ros2_control ─────────────────────────────────────
    urdf_path = PathJoinSubstitution(
        [FindPackageShare("guide_robot_description"), "urdf", "guide_robot.urdf.xacro"]
    )
    robot_description = ParameterValue(
        Command(
            [
                FindExecutable(name="xacro"),
                " ",
                urdf_path,
                " use_mock_hardware:=",
                use_mock_hardware,
            ]
        ),
        value_type=str,
    )
    controllers_path = PathJoinSubstitution([pkg_description, "config", "controllers.yaml"])
    rviz_config = PathJoinSubstitution([pkg_bringup, "rviz", "hardware.rviz"])

    robot_state_publisher_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[
            {
                "robot_description": robot_description,
                "use_sim_time": use_sim_time,
            }
        ],
    )

    controller_manager_node = Node(
        package="controller_manager",
        executable="ros2_control_node",
        parameters=[
            {"robot_description": robot_description},
            controllers_path,
        ],
        remappings=[("/diff_drive_controller/odom", "/odom")],
    )

    diff_drive_controller = Node(
        package="controller_manager", executable="spawner", arguments=["diff_drive_controller"]
    )

    joint_state_broadcaster = Node(
        package="controller_manager", executable="spawner", arguments=["joint_state_broadcaster"]
    )

    # ── Перцепция ────────────────────────────────────────────────────────────
    perception = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "perception.launch.py")),
        condition=IfCondition(launch_sensors),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "real_lidars": "true",
            "launch_sonar": launch_sonar,
            "merge_frame": "base_footprint",
        }.items(),
    )

    # ── Навигация ────────────────────────────────────────────────────────────
    nav_stack = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "nav_stack.launch.py")),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "nav": nav,
            "slam": slam,
            "map": map_yaml_file,
            "nav_params_file": nav_params_file,
            "slam_params_file": slam_params_file,
            "autostart_nav": autostart_nav,
            "launch_supervisor": "true",
            "autostart_supervisor": autostart_supervisor,
        }.items(),
    )

    # ── Стек экскурсий ───────────────────────────────────────────────────────
    high_level_stack = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_bringup, "launch", "high_level_stack.launch.py")
        ),
        condition=IfCondition(launch_high_level),
        launch_arguments={"use_sim_time": use_sim_time}.items(),
    )

    # ── Tooling ──────────────────────────────────────────────────────────────
    foxglove_bridge_node = Node(
        package="foxglove_bridge",
        executable="foxglove_bridge",
        name="foxglove_bridge",
        output="screen",
        condition=IfCondition(launch_foxglove),
        parameters=[
            {
                "port": 8765,
                "address": "0.0.0.0",
                "send_buffer_limit": 100000000,
                "use_sim_time": use_sim_time,
            }
        ],
    )

    rviz_node = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        output="screen",
        arguments=["-d", rviz_config],
        condition=IfCondition(launch_rviz),
        parameters=[{"use_sim_time": use_sim_time}],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_mock,
            declare_launch_sensors,
            declare_launch_sonar,
            declare_nav,
            declare_slam,
            declare_map,
            declare_nav_params,
            declare_slam_params,
            declare_autostart_nav,
            declare_autostart_supervisor,
            declare_launch_high_level,
            declare_launch_foxglove,
            declare_launch_rviz,
            robot_state_publisher_node,
            controller_manager_node,
            diff_drive_controller,
            joint_state_broadcaster,
            perception,
            nav_stack,
            high_level_stack,
            foxglove_bridge_node,
            rviz_node,
        ]
    )