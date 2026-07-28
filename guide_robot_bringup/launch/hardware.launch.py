import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.conditions import IfCondition, UnlessCondition
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
    pkg_slam_toolbox = get_package_share_directory("slam_toolbox")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_use_sim_time = DeclareLaunchArgument(
        name="use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_mock = DeclareLaunchArgument(
        name="use_mock_hardware",
        default_value="false",
        description="Launch robot without hardware",
    )
    declare_launch_sensors = DeclareLaunchArgument(
        name="launch_sensors",
        default_value="true",
        description="Launch lidar sensors and scan merger",
    )
    declare_launch_sonar = DeclareLaunchArgument(
        name="launch_sonar",
        default_value="true",
        description="Launch sonar range node",
    )
    declare_launch_foxglove = DeclareLaunchArgument(
        name="launch_foxglove",
        default_value="true",
        description="Launch Foxglove Bridge",
    )
    declare_slam = DeclareLaunchArgument(
        name="slam",
        default_value="false",
        description="true — строить карту SLAM Toolbox; false — AMCL по готовой карте из map",
    )
    declare_slam_params = DeclareLaunchArgument(
        name="slam_params_file",
        default_value=os.path.join(pkg_navigation, "config", "mapper_params_online_async.yaml"),
        description="Full path to SLAM Toolbox parameters file",
    )
    declare_map = DeclareLaunchArgument(
        name="map",
        default_value=os.path.join(pkg_navigation, "map", "lab_map.yaml"),
        description="Готовая карта для режима slam:=false (map_server + AMCL)",
    )
    declare_nav = DeclareLaunchArgument(
        name="nav",
        default_value="true",
        description="Launch Nav2 stack (planner, controller, behaviors, collision_monitor)",
    )
    declare_nav_params = DeclareLaunchArgument(
        name="nav_params_file",
        default_value=os.path.join(pkg_navigation, "config", "first_iter_nav2.yaml"),
        description="Full path to Nav2 parameters file",
    )
    declare_launch_rviz = DeclareLaunchArgument(
        name="launch_rviz",
        default_value="true",
        description="Launch RViz (requires a display; keep off on the headless robot)",
    )
    declare_autostart = DeclareLaunchArgument(
        name="autostart",
        default_value="true",
        description="Autostart Nav2 lifecycle nodes",
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    use_mock_hardware = LaunchConfiguration("use_mock_hardware")
    launch_sensors = LaunchConfiguration("launch_sensors")
    launch_sonar = LaunchConfiguration("launch_sonar")
    launch_foxglove = LaunchConfiguration("launch_foxglove")
    slam = LaunchConfiguration("slam")
    slam_params_file = LaunchConfiguration("slam_params_file")
    map_yaml_file = LaunchConfiguration("map")
    nav = LaunchConfiguration("nav")
    nav_params_file = LaunchConfiguration("nav_params_file")
    launch_rviz = LaunchConfiguration("launch_rviz")
    autostart = LaunchConfiguration("autostart")

    # ── Robot Description & Hardware ──────────────────────────────────────────
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

    controllers_path = PathJoinSubstitution(
        [FindPackageShare("guide_robot_bringup"), "config", "guide_robot_controllers.yaml"]
    )

    rviz_config = PathJoinSubstitution(
        [FindPackageShare("guide_robot_bringup"), "rviz", "view_robot.rviz"]
    )

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

    # ── Sensors Launch (Lidars + Scan Merger) ──────────────────────────────────
    sensors_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "sensors.launch.py")),
        condition=IfCondition(launch_sensors),
        launch_arguments={"use_sim_time": use_sim_time}.items(),
    )

    # ── Sonar Node ─────────────────────────────────────────────────────────────
    sonar_node = Node(
        package="guide_robot_sonar",
        executable="sonar_node_mult.py",
        name="sonar_node",
        output="screen",
        condition=IfCondition(launch_sonar),
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "publish_inf_as_out_of_range": True,
            }
        ],
    )

    # ── Foxglove Bridge Node ───────────────────────────────────────────────────
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

    # ── Navigation & SLAM Stack (matching simulation.launch.py) ───────────────
    # Uses guide_robot_navigation launch files which include common.launch.py
    # (Nav2 + nav2_collision_monitor + lifecycle_manager_safety).
    nav_group = GroupAction(
        condition=IfCondition(nav),
        actions=[
            # slam:=true -> SLAM Toolbox + Nav2 + collision_monitor
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_navigation, "launch", "slam_navigation.launch.py")
                ),
                condition=IfCondition(slam),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart": autostart,
                    "slam_params_file": slam_params_file,
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
            # slam:=false -> AMCL + map_server + Nav2 + collision_monitor
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_navigation, "launch", "navigation.launch.py")
                ),
                condition=UnlessCondition(slam),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart": autostart,
                    "map": map_yaml_file,
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
        ],
    )

    # Standalone SLAM when nav:=false and slam:=true
    slam_only = GroupAction(
        condition=UnlessCondition(nav),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_slam_toolbox, "launch", "online_async_launch.py")
                ),
                condition=IfCondition(slam),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "slam_params_file": slam_params_file,
                }.items(),
            ),
        ],
    )

    # Wait 10s for hardware controllers, sensors, and TF tree to initialize before starting Nav2
    delayed_nav = TimerAction(period=10.0, actions=[nav_group, slam_only])

    # ── RViz2 (Optional) ───────────────────────────────────────────────────────
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
            declare_launch_foxglove,
            declare_slam,
            declare_slam_params,
            declare_map,
            declare_nav,
            declare_nav_params,
            declare_launch_rviz,
            declare_autostart,
            robot_state_publisher_node,
            controller_manager_node,
            diff_drive_controller,
            joint_state_broadcaster,
            sensors_launch,
            sonar_node,
            foxglove_bridge_node,
            delayed_nav,
            rviz_node,
        ]
    )
