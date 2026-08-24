# Голос + LLM без моторов / Nav2 / ros2_control.
# hardware.launch.py падает, если реле драйвера снято — этот файл нет.
#
#   ros2 launch guide_robot_bringup desk.launch.py
#
# Тур из болтовни не поедет: NavigateToPose некому исполнить.

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """Voice + semantic_map + mission + llm, no drivers."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")
    pkg_voice = get_package_share_directory("guide_robot_voice")
    pkg_llm = get_package_share_directory("guide_robot_llm")

    declare_autostart = DeclareLaunchArgument(
        "autostart",
        default_value="true",
        description="Self-activate lifecycle managers (no supervisor on the desk)",
    )
    autostart = LaunchConfiguration("autostart")

    high_level = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_bringup, "launch", "high_level_stack.launch.py")
        ),
        launch_arguments={
            "autostart": autostart,
            "voice_params_file": os.path.join(pkg_voice, "config", "voice_jetson.yaml"),
        }.items(),
    )
    llm = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_llm, "launch", "llm.launch.py")),
        launch_arguments={"autostart": autostart}.items(),
    )
    return LaunchDescription([declare_autostart, high_level, llm])
