# =========================================================================
#  simulation.launch.py - Guide robot in Gazebo Classic (empty world).
#
#  Structure follows a known-working pattern:
#    - robot_state_publisher first
#    - spawn_entity next
#    - gazebo LAST (this ordering matters: gazebo_ros2_control resolves
#      robot_description from robot_state_publisher at plugin load time)
#    - NO controller spawners here - load them manually after startup:
#        ros2 run controller_manager spawner joint_state_broadcaster
#        ros2 run controller_manager spawner diff_drive_controller
#
#  Run:
#    ros2 launch guide_robot_simulation simulation.launch.py
#  Teleop:
#    ros2 run teleop_twist_keyboard teleop_twist_keyboard \
#      --ros-args -r /cmd_vel:=/diff_drive_controller/cmd_vel
# =========================================================================

import os
import re
import subprocess
from os import pathsep
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    RegisterEventHandler,
    SetEnvironmentVariable,
    TimerAction,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch Gazebo."""
    pkg_description = get_package_share_directory("guide_robot_description")
    pkg_simulation = get_package_share_directory("guide_robot_simulation")

    # --- Arguments ---
    use_sim_time_arg = DeclareLaunchArgument(
        name="use_sim_time",
        default_value="true",
        description="Use simulation clock if true",
    )

    world_arg = DeclareLaunchArgument(
        name="world",
        default_value=os.path.join(pkg_simulation, "worlds", "simple.world"),
        description="Path to the Gazebo world file",
    )

    model_arg = DeclareLaunchArgument(
        name="model",
        default_value=os.path.join(pkg_description, "urdf", "guide_robot.urdf.xacro"),
        description="Absolute path to robot urdf file",
    )

    # --- Gazebo model search path (meshes) ---
    model_path = str(Path(pkg_description).parent.resolve())
    model_path += pathsep + os.path.join(pkg_description, "meshes")

    gazebo_model_path = SetEnvironmentVariable("GAZEBO_MODEL_PATH", model_path)

    # Avoid long timeouts when the container has no internet access
    gazebo_no_online_db = SetEnvironmentVariable("GAZEBO_MODEL_DATABASE_URI", "")

    # --- robot_description ---
    xacro_out = subprocess.run(
        [
            "xacro",
            os.path.join(pkg_description, "urdf", "guide_robot.urdf.xacro"),
            "use_sim:=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    robot_description = re.sub(r"<!--.*?-->", "", xacro_out, flags=re.S)

    robot_state_publisher_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[
            {
                "robot_description": robot_description,
                "use_sim_time": LaunchConfiguration("use_sim_time"),
            }
        ],
    )

    # --- Spawn robot from the /robot_description topic ---
    spawn_entity = Node(
        package="gazebo_ros",
        executable="spawn_entity.py",
        output="screen",
        arguments=[
            "-topic",
            "robot_description",
            "-entity",
            "guide_robot",
            "-z",
            "0.15",
        ],
    )

    # --- Gazebo (empty world) ---
    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            [
                os.path.join(get_package_share_directory("gazebo_ros"), "launch"),
                "/gazebo.launch.py",
            ]
        ),
        launch_arguments={"world": LaunchConfiguration("world")}.items(),
    )

    # --- Controller spawners ---
    joint_state_broadcaster_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_state_broadcaster"],
        output="screen",
    )

    diff_drive_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["diff_drive_controller", "--controller-manager", "/controller_manager"],
    )

    # JSB после запуска gazebo, diff_drive - после JSB
    delayed_jsb = TimerAction(period=2.0, actions=[joint_state_broadcaster_spawner])

    diff_drive_after_jsb = RegisterEventHandler(
        OnProcessExit(
            target_action=joint_state_broadcaster_spawner,
            on_exit=[diff_drive_spawner],
        )
    )

    cmd_vel_relay = Node(
        package="topic_tools",
        executable="relay",
        arguments=["/cmd_vel", "/diff_drive_controller/cmd_vel_unstamped"],
        output="screen",
    )

    rqt_robot_steering = Node(
        package="rqt_robot_steering",
        executable="rqt_robot_steering",
        name="rqt_robot_steering",
        output="screen",
    )

    sonar_merge = Node(
        package="guide_robot_simulation",
        executable="sonar_merge",
        name="sonar_merge",
        output="screen",
        parameters=[
            {
                "use_sim_time": True,
                "sensor_ids": [
                    "sonar_1",
                    "sonar_2",
                    "sonar_4",
                    "sonar_5",
                    "sonar_6",
                    "sonar_8",
                    "sonar_9",
                ],
            }
        ],
    )

    return LaunchDescription(
        [
            use_sim_time_arg,
            model_arg,
            world_arg,
            gazebo_model_path,
            gazebo_no_online_db,
            robot_state_publisher_node,
            spawn_entity,
            gazebo,
            delayed_jsb,
            diff_drive_after_jsb,
            cmd_vel_relay,
            rqt_robot_steering,
            sonar_merge,
        ]
    )
