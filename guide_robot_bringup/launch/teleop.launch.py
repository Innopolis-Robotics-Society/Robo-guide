from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch teleop_twist_keyboard in safe or admin-bypass mode."""
    declare_full_control = DeclareLaunchArgument(
        "full_control",
        default_value="false",
        description="true — admin bypass (обходит e_stop и collision_monitor)",
    )

    full_control = LaunchConfiguration("full_control")

    teleop_safe = Node(
        package="teleop_twist_keyboard",
        executable="teleop_twist_keyboard",
        name="teleop",
        output="screen",
        prefix="xterm -e",
        remappings=[("cmd_vel", "/safety_cmd_vel")],
        condition=UnlessCondition(full_control),
    )

    teleop_admin = Node(
        package="teleop_twist_keyboard",
        executable="teleop_twist_keyboard",
        name="teleop",
        output="screen",
        prefix="xterm -e",
        remappings=[("cmd_vel", "/admin_cmd_vel")],
        condition=IfCondition(full_control),
    )

    return LaunchDescription(
        [
            declare_full_control,
            teleop_safe,
            teleop_admin,
        ]
    )
