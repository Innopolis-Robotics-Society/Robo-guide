import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Generate the launch description for the Guide Robot hardware stack."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_navigation = get_package_share_directory("guide_robot_navigation")
    pkg_slam_toolbox = get_package_share_directory("slam_toolbox")
    pkg_nav2_bringup = get_package_share_directory("nav2_bringup")

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
        default_value="true",
        description="true — строить карту SLAM Toolbox; false — AMCL по готовой карте из map",
    )
    declare_slam_params = DeclareLaunchArgument(
        name="slam_params_file",
        default_value=os.path.join(pkg_navigation, "params", "mapper_params_online_async.yaml"),
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
        description="Launch Nav2 (planner, controller, behaviors, bt_navigator)",
    )
    declare_nav_params = DeclareLaunchArgument(
        name="nav_params_file",
        default_value=os.path.join(pkg_navigation, "params", "first_iter_nav2.yaml"),
        description="Full path to Nav2 parameters file",
    )
    declare_launch_rviz = DeclareLaunchArgument(
        name="launch_rviz",
        default_value="true",
        description="Launch RViz (requires a display; keep off on the headless robot)",
    )
    declare_cmd_vel_relay = DeclareLaunchArgument(
        name="cmd_vel_relay",
        default_value="true",
        description="Relay /cmd_vel to diff_drive_controller (needed by teleop_twist_keyboard)",
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
    cmd_vel_relay = LaunchConfiguration("cmd_vel_relay")

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

    # diff_drive_controller публикует одометрию в ~/odom, то есть в
    # /diff_drive_controller/odom. Ремапа не было, поэтому топика /odom не
    # существовало вовсе, хотя bt_navigator (odom_topic: /odom) и OdomSmoother
    # controller_server-а его ждут: DWB каждый цикл считал, что робот стоит.
    # Навигация при этом работала, потому что TF odom->base_footprint контроллер
    # публикует сам, а SLAM живёт на TF, а не на топике.
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

    # ── cmd_vel bridge ─────────────────────────────────────────────────────────
    # Nothing in this repo connected /cmd_vel to the controller, so teleop_twist_keyboard
    # (which publishes a plain Twist on /cmd_vel) drove nothing: the wheels never turned,
    # odom stayed at the origin and odom->base_footprint never moved.
    # guide_robot_controllers.yaml sets use_stamped_vel: false, which on Humble makes
    # diff_drive_controller listen on ~/cmd_vel_unstamped (Twist) rather than ~/cmd_vel
    # (TwistStamped) — hence this exact target topic.
    cmd_vel_relay_node = Node(
        package="topic_tools",
        executable="relay",
        name="cmd_vel_relay",
        output="screen",
        condition=IfCondition(cmd_vel_relay),
        arguments=["/cmd_vel", "/diff_drive_controller/cmd_vel_unstamped"],
        parameters=[{"use_sim_time": use_sim_time}],
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
        executable="sonar_node.py",
        name="sonar_node",
        output="screen",
        condition=IfCondition(launch_sonar),
        parameters=[{"use_sim_time": use_sim_time}],
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

    # ── SLAM (Mapping) ─────────────────────────────────────────────────────────
    slam_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_slam_toolbox, "launch", "online_async_launch.py")
        ),
        condition=IfCondition(slam),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "slam_params_file": slam_params_file,
        }.items(),
    )

    # ── Локализация по готовой карте (альтернатива SLAM) ───────────────────────
    # localization_launch.py = map_server + AMCL. map->odom даёт AMCL вместо
    # slam_toolbox, поэтому режимы взаимоисключающи: slam:=true строит карту,
    # slam:=false ездит по готовой. Путь к карте подставляется в
    # map_server.yaml_filename самим localization_launch.py (RewrittenYaml),
    # значение в first_iter_nav2.yaml роли не играет.
    #
    # AMCL стартует с облаком частиц вокруг (0, 0) — задать реальную позу можно
    # либо "2D Pose Estimate" в RViz, либо set_initial_pose в секции amcl
    # (guide_robot_navigation/params/first_iter_nav2.yaml).
    localization_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_nav2_bringup, "launch", "localization_launch.py")
        ),
        condition=UnlessCondition(slam),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "params_file": nav_params_file,
            "map": map_yaml_file,
        }.items(),
    )

    # ── Nav2 ───────────────────────────────────────────────────────────────────
    # Без этого RViz "2D Goal Pose" публиковал /goal_pose в пустоту: bt_navigator
    # не запускался нигде, кроме simulation.launch.py.
    # navigation_launch.py = planner/controller/behaviors/bt_navigator без
    # локализации; map->odom даёт slam_toolbox выше.
    nav2_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_nav2_bringup, "launch", "navigation_launch.py")
        ),
        condition=IfCondition(nav),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "params_file": nav_params_file,
        }.items(),
    )
    # Nav2 и AMCL нужно готовое TF-дерево и /scan до конфигурации нод.
    delayed_nav2 = TimerAction(period=10.0, actions=[localization_launch, nav2_launch])

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
            declare_cmd_vel_relay,
            robot_state_publisher_node,
            controller_manager_node,
            diff_drive_controller,
            joint_state_broadcaster,
            cmd_vel_relay_node,
            sensors_launch,
            sonar_node,
            foxglove_bridge_node,
            slam_launch,
            delayed_nav2,
            rviz_node,
        ]
    )
