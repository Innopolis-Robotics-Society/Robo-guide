"""Стенд XVF3800: единый ALSA-владелец и TTS через RemoteSink.

Этот launch намеренно не поднимает VAD/ASR: сначала отдельно доказываем
playback, фактический SpeakingStatus, отмену и непрерывность capture.
"""

from launch_ros.actions import LifecycleNode, Node
from launch_ros.parameter_descriptions import ParameterFile, ParameterValue
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution


def generate_launch_description() -> LaunchDescription:
    """Собрать минимальный full-duplex TTS-стенд."""
    audio_params = LaunchConfiguration("audio_params")
    voice_params = LaunchConfiguration("voice_params")
    xvf_voice_params = LaunchConfiguration("xvf_voice_params")
    tts_backend = LaunchConfiguration("tts_backend")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "audio_params",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("guide_robot_audio"), "config", "xvf3800.yaml"]
                ),
            ),
            DeclareLaunchArgument(
                "voice_params",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("guide_robot_voice"), "config", "voice.yaml"]
                ),
            ),
            DeclareLaunchArgument(
                "xvf_voice_params",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("guide_robot_voice"), "config", "voice_xvf3800.yaml"]
                ),
            ),
            DeclareLaunchArgument("tts_backend", default_value="silero"),
            LifecycleNode(
                package="guide_robot_audio",
                executable="xvf3800_audio_node",
                name="xvf3800_audio_node",
                namespace="",
                output="screen",
                parameters=[ParameterFile(audio_params, allow_substs=True)],
            ),
            Node(
                package="guide_robot_voice",
                executable="tts_node",
                name="tts_node",
                output="screen",
                parameters=[
                    ParameterFile(voice_params, allow_substs=True),
                    ParameterFile(xvf_voice_params, allow_substs=True),
                    {"backend": ParameterValue(tts_backend, value_type=str)},
                ],
            ),
            Node(
                package="nav2_lifecycle_manager",
                executable="lifecycle_manager",
                name="lifecycle_manager_xvf3800_tts",
                output="screen",
                parameters=[
                    {
                        "autostart": True,
                        # Активация строго hardware owner -> клиент TTS.
                        # При shutdown lifecycle_manager использует обратный порядок.
                        "node_names": ["xvf3800_audio_node", "tts_node"],
                        "bond_timeout": 0.0,
                    }
                ],
            ),
        ]
    )
