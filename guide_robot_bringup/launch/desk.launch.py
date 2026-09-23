# Голос + LLM без моторов / Nav2 / ros2_control.
# hardware.launch.py падает, если реле драйвера снято — этот файл нет.
#
#   ros2 launch guide_robot_bringup desk.launch.py
#   ros2 launch guide_robot_bringup desk.launch.py voice_profile:=xvf3800
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
    declare_voice_profile = DeclareLaunchArgument(
        "voice_profile",
        default_value="legacy",
        choices=["legacy", "xvf3800"],
        description="Voice hardware profile; xvf3800 does not launch audio_frontend",
    )
    declare_launch_face = DeclareLaunchArgument(
        "launch_face",
        default_value="true",
        description="Launch the face HTTP/SVG node",
    )
    declare_tts_backend = DeclareLaunchArgument(
        "tts_backend",
        default_value="silero",
        choices=["silero", "piper", "null"],
        description="TTS backend used by the XVF profile",
    )
    declare_automatic_barge_in_enabled = DeclareLaunchArgument(
        "automatic_barge_in_enabled",
        default_value="false",
        description="Enable automatic barge-in for an explicitly validated test profile",
    )
    declare_audio_profile_validated = DeclareLaunchArgument(
        "audio_profile_validated",
        default_value="false",
        description="Confirm the current acoustic profile for automatic barge-in",
    )
    autostart = LaunchConfiguration("autostart")
    voice_profile = LaunchConfiguration("voice_profile")
    launch_face = LaunchConfiguration("launch_face")
    tts_backend = LaunchConfiguration("tts_backend")
    automatic_barge_in_enabled = LaunchConfiguration("automatic_barge_in_enabled")
    audio_profile_validated = LaunchConfiguration("audio_profile_validated")

    high_level = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_bringup, "launch", "high_level_stack.launch.py")
        ),
        launch_arguments={
            "autostart": autostart,
            # Used only by legacy. XVF has a separate base-file argument and
            # therefore cannot accidentally inherit USB/Pulse parameters.
            "voice_params_file": os.path.join(pkg_voice, "config", "voice_jetson.yaml"),
            "voice_profile": voice_profile,
            "launch_face": launch_face,
            "tts_backend": tts_backend,
            "automatic_barge_in_enabled": automatic_barge_in_enabled,
            "audio_profile_validated": audio_profile_validated,
        }.items(),
    )
    llm = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_llm, "launch", "llm.launch.py")),
        launch_arguments={"autostart": autostart}.items(),
    )
    return LaunchDescription(
        [
            declare_autostart,
            declare_voice_profile,
            declare_launch_face,
            declare_tts_backend,
            declare_automatic_barge_in_enabled,
            declare_audio_profile_validated,
            high_level,
            llm,
        ]
    )
