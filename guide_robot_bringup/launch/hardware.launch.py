from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

def generate_launch_description():
    """Generate the launch description for the Guide Robot hardware stack."""
    # parameters
    declare_use_sim_time = DeclareLaunchArgument(
        name="use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    use_sim_time = LaunchConfiguration("use_sim_time")

    declare_mock = DeclareLaunchArgument(
        name="use_mock_hardware",
        default_value="false",
        description="Launch robot without hardware",
    )

    use_mock_hardware = LaunchConfiguration("use_mock_hardware")

    urdf_path = PathJoinSubstitution(
        [FindPackageShare("guide_robot_description"), "urdf", "guide_robot.urdf.xacro"]
    )

    robot_description = Command(
        [FindExecutable(name="xacro"), " ", urdf_path, " use_mock_hardware:=", use_mock_hardware]
    )

    controllers_path = PathJoinSubstitution(
        [FindPackageShare("guide_robot_bringup"), "config", "guide_robot_controllers.yaml"]
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

    rviz_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution(
                [FindPackageShare("guide_robot_bringup"), "launch", "view_robot.launch.py"]
            )
        )
    )
    controller_manager_node = Node(
        package="controller_manager",
        executable="ros2_control_node",
        parameters=[
            {"robot_description": robot_description},
            controllers_path,
        ],
    )

    diff_drive_controller = Node(
        package="controller_manager", executable="spawner", arguments=["diff_drive_controller"]
    )

    joint_state_broadcaster = Node(
        package="controller_manager", executable="spawner", arguments=["joint_state_broadcaster"]
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_mock,
            robot_state_publisher_node,
            controller_manager_node,
            diff_drive_controller,
            joint_state_broadcaster,
            rviz_launch,
        ]
    )
