"""Полный компьютерный стенд XVF3800 без legacy `audio_frontend`.

Единственный источник `/audio/mic` -- `xvf3800_audio_node`. Автоматический
barge-in остаётся закрыт параметром `require_aec_for_barge_in`, пока AEC и
processed-канал не подтверждены измерениями.
"""

from launch_ros.actions import LifecycleNode, Node
from launch_ros.parameter_descriptions import ParameterFile, ParameterValue
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution

VOICE_NODES = [
    "tts_node",
    "vad_node",
    "voice_session_manager",
    "wakeword_node",
    "asr_node",
]
BRINGUP_ORDER = ["xvf3800_audio_node", *VOICE_NODES]


def generate_launch_description() -> LaunchDescription:
    """Собрать full-duplex voice-стенд с единственным ALSA-владельцем."""
    audio_params = LaunchConfiguration("audio_params")
    voice_params = LaunchConfiguration("voice_params")
    xvf_voice_params = LaunchConfiguration("xvf_voice_params")
    tts_backend = LaunchConfiguration("tts_backend")
    autostart = LaunchConfiguration("autostart")
    publish_stereo_debug = LaunchConfiguration("publish_stereo_debug")
    automatic_barge_in_enabled = LaunchConfiguration("automatic_barge_in_enabled")
    audio_profile_validated = LaunchConfiguration("audio_profile_validated")

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
            "xvf_voice_params",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_voice"), "config", "voice_xvf3800.yaml"]
            ),
        ),
        DeclareLaunchArgument("tts_backend", default_value="silero"),
        DeclareLaunchArgument("autostart", default_value="true"),
        DeclareLaunchArgument("publish_stereo_debug", default_value="false"),
        DeclareLaunchArgument(
            "automatic_barge_in_enabled",
            default_value="false",
            description="Enable session-manager barge-in; keep"
            " false until the audio path is tested",
        ),
        DeclareLaunchArgument(
            "audio_profile_validated",
            default_value="false",
            description="Confirm that the current acoustic profile is safe for automatic barge-in",
        ),
    ]

    audio_owner = LifecycleNode(
        package="guide_robot_audio",
        executable="xvf3800_audio_node",
        name="xvf3800_audio_node",
        namespace="",
        output="screen",
        parameters=[
            ParameterFile(audio_params, allow_substs=True),
            {"publish_stereo_debug": ParameterValue(publish_stereo_debug, value_type=bool)},
        ],
    )

    voice_nodes = []
    for name in VOICE_NODES:
        parameters: list[object] = [
            ParameterFile(voice_params, allow_substs=True),
            ParameterFile(xvf_voice_params, allow_substs=True),
        ]
        if name == "tts_node":
            parameters.append({"backend": ParameterValue(tts_backend, value_type=str)})
        if name == "voice_session_manager":
            # These explicit launch overrides are intentionally separate.
            # A developer may enable both on a headphone stand, while the
            # production XVF profile remains fail-closed until loudspeaker/AEC
            # validation has actually been completed.
            parameters.append(
                {
                    "automatic_barge_in_enabled": ParameterValue(
                        automatic_barge_in_enabled, value_type=bool
                    ),
                    "audio_profile_validated": ParameterValue(
                        audio_profile_validated, value_type=bool
                    ),
                }
            )
        remappings = []
        if name == "wakeword_node":
            # В managed-профиле wakeword видит также KWS_ONLY, а обычные
            # потребители /asr/partial -- только ADMIT-реплики.
            remappings = [("/asr/partial", "/voice/kws_partial")]
        voice_nodes.append(
            Node(
                package="guide_robot_voice",
                executable=name,
                name=name,
                output="screen",
                parameters=parameters,
                remappings=remappings,
            )
        )

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        # Тот же контракт, что у legacy voice.launch.py: supervisor
        # может менять профиль, не меняя manager service name.
        name="lifecycle_manager_voice",
        output="screen",
        parameters=[
            {
                "autostart": autostart,
                "node_names": BRINGUP_ORDER,
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription([*arguments, audio_owner, *voice_nodes, manager])
