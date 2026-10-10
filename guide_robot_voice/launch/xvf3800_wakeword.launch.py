"""Проверка frozen Firaya KWS на XVF без VAD, ASR, TTS, FSM и LLM."""

from launch_ros.actions import LifecycleNode, Node
from launch_ros.parameter_descriptions import ParameterFile, ParameterValue
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution


def generate_launch_description() -> LaunchDescription:
    """Собрать trigger-only стенд без ASR, TTS и диалога."""
    arguments = [
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
            "wakeword_params",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_voice"), "config", "voice_wakeword.yaml"]
            ),
        ),
        DeclareLaunchArgument("autostart", default_value="true"),
    ]
    audio = LifecycleNode(
        package="guide_robot_audio",
        executable="xvf3800_audio_node",
        name="xvf3800_audio_node",
        namespace="",
        output="screen",
        parameters=[ParameterFile(LaunchConfiguration("audio_params"), allow_substs=True)],
    )
    wakeword = Node(
        package="guide_robot_voice",
        executable="wakeword_node",
        name="wakeword_node",
        output="screen",
        parameters=[
            ParameterFile(LaunchConfiguration("voice_params"), allow_substs=True),
            ParameterFile(LaunchConfiguration("wakeword_params"), allow_substs=True),
            {"backend": "oww"},
        ],
        # Чужие ASR/stop-события не должны попадать в изолированный KWS-тест.
        remappings=[
            ("/asr/partial", "/wakeword_test/asr/partial"),
            ("/asr/transcript", "/wakeword_test/asr/transcript"),
            ("/speech/wakeword", "/wakeword_test/speech/wakeword"),
            ("/speech/cancel_all", "/wakeword_test/speech/cancel_all"),
        ],
    )
    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_wakeword_test",
        output="screen",
        parameters=[
            {
                "autostart": ParameterValue(LaunchConfiguration("autostart"), value_type=bool),
                "node_names": ["xvf3800_audio_node", "wakeword_node"],
                "bond_timeout": 0.0,
            }
        ],
    )
    return LaunchDescription([*arguments, audio, wakeword, manager])
