# =========================================================================
#  high_level_stack.launch.py — единая точка входа для стека экскурсий.
#
#  Аудио и высокоуровневый служебный слой поверх нав-стека:
#    guide_robot_voice          — микрофон/динамик, ASR/VAD/wakeword, TTS
#    guide_robot_semantic_map   — route_server + content/location/route_planner
#    guide_robot_mission_control — mission_fsm + narration_server + presence_monitor
#    guide_robot_face           — HTTP+SVG лицо на DP-панели (не lifecycle)
#
#  Отдельно от nav_stack.launch.py (там только safety/localization/
#  navigation/супервизор) -- собирается по образцу того же файла, но
#  сознательно не включается из него: nav-стек обязан подниматься и без
#  экскурсионного слоя (например, для чистого картирования/локализации).
#
#  Каждая из трёх groups в guide_robot_supervisor (voice/semantic_map/
#  mission, requires: [navigation, voice, semantic_map] у mission) ждёт,
#  что её lifecycle_manager уже существует как процесс -- этот launch-файл
#  их поднимает с autostart:=false по умолчанию, bring-up делает супервизор
#  (см. guide_robot_supervisor/config/supervisor.yaml).
#
#  Usage:
#    ros2 launch guide_robot_bringup high_level_stack.launch.py
#    ros2 launch guide_robot_bringup high_level_stack.launch.py launch_voice:=false
#    ros2 launch guide_robot_bringup high_level_stack.launch.py launch_face:=false
#    # standalone-тест без супервизора (каждый пакет сам себя поднимает):
#    ros2 launch guide_robot_bringup high_level_stack.launch.py autostart:=true
#
#  ГРАБЛЯ (воспроизведено вживую через simulation.launch.py): voice/
#  semantic_map/mission каждый сам объявляет `params_file` со своим
#  дефолтом (DeclareLaunchArgument применяет default ТОЛЬКО если имя ещё
#  не установлено где-либо в дереве). `gazebo_ros/launch/gzserver.launch.py`
#  тоже объявляет `params_file` (default="", для СВОЕГО, не связанного
#  --params-file у gzserver) -- когда этот файл подключается вместе с
#  nav_stack.launch.py (там GroupAction(scoped=True) для нав2 корректно
#  выставляет `params_file` ВНУТРИ своей области, но откатывает обратно на
#  '' при выходе из скоупа), к моменту, когда сюда доходит очередь,
#  `params_file` в общем (плоском!) launch-контексте уже == "" -- и наши
#  ноды получают `Path("")` == `Path(".")` -> `IsADirectoryError` при
#  открытии как yaml. Поэтому здесь `params_file` передаётся ЯВНО в каждый
#  include (SetLaunchConfiguration всегда перезаписывает, в отличие от
#  DeclareLaunchArgument) -- тем же способом, каким
#  guide_robot_navigation/launch/navigation.launch.py передаёт nav2 его
#  params_file. Плюс каждый include завёрнут в scoped GroupAction (default
#  scoped=True), чтобы ничего из ЭТИХ трёх не утекло дальше по дереву.
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression


def generate_launch_description():
    """Launch the tour stack (voice + semantic_map + mission_control + face)."""
    pkg_voice = get_package_share_directory("guide_robot_voice")
    pkg_audio = get_package_share_directory("guide_robot_audio")
    pkg_semantic_map = get_package_share_directory("guide_robot_semantic_map")
    pkg_mission_control = get_package_share_directory("guide_robot_mission_control")
    pkg_face = get_package_share_directory("guide_robot_face")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_launch_voice = DeclareLaunchArgument(
        "launch_voice", default_value="true", description="Launch guide_robot_voice"
    )
    declare_launch_semantic_map = DeclareLaunchArgument(
        "launch_semantic_map", default_value="true", description="Launch guide_robot_semantic_map"
    )
    declare_launch_mission = DeclareLaunchArgument(
        "launch_mission", default_value="true", description="Launch guide_robot_mission_control"
    )
    declare_launch_face = DeclareLaunchArgument(
        "launch_face", default_value="true", description="Launch guide_robot_face (HTTP+SVG kiosk)"
    )
    declare_autostart = DeclareLaunchArgument(
        "autostart",
        default_value="false",
        description="Autostart all three lifecycle_manager_* (bypasses guide_robot_supervisor "
        "-- for standalone testing only, the supervisor normally owns bring-up)",
    )
    declare_voice_params_file = DeclareLaunchArgument(
        "voice_params_file",
        default_value=os.path.join(pkg_voice, "config", "voice.yaml"),
        description="YAML for the legacy voice profile",
    )
    declare_voice_profile = DeclareLaunchArgument(
        "voice_profile",
        default_value="legacy",
        choices=["legacy", "xvf3800"],
        description="legacy uses audio_frontend; xvf3800 uses the single ALSA audio owner",
    )
    declare_xvf_audio_params_file = DeclareLaunchArgument(
        "xvf_audio_params_file",
        default_value=os.path.join(pkg_audio, "config", "xvf3800.yaml"),
        description="Hardware YAML for xvf3800_audio_node",
    )
    declare_xvf_base_voice_params_file = DeclareLaunchArgument(
        "xvf_base_voice_params_file",
        default_value=os.path.join(pkg_voice, "config", "voice.yaml"),
        description="Base voice YAML loaded before the XVF-specific overrides",
    )
    declare_xvf_voice_params_file = DeclareLaunchArgument(
        "xvf_voice_params_file",
        default_value=os.path.join(pkg_voice, "config", "voice_xvf3800.yaml"),
        description="XVF-specific overrides applied after xvf_base_voice_params_file",
    )
    declare_tts_backend = DeclareLaunchArgument(
        "tts_backend",
        default_value="silero",
        choices=["silero", "piper", "null"],
        description="TTS backend passed to the XVF voice profile",
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    launch_voice = LaunchConfiguration("launch_voice")
    launch_semantic_map = LaunchConfiguration("launch_semantic_map")
    launch_mission = LaunchConfiguration("launch_mission")
    launch_face = LaunchConfiguration("launch_face")
    autostart = LaunchConfiguration("autostart")
    voice_params_file = LaunchConfiguration("voice_params_file")
    voice_profile = LaunchConfiguration("voice_profile")
    xvf_audio_params_file = LaunchConfiguration("xvf_audio_params_file")
    xvf_base_voice_params_file = LaunchConfiguration("xvf_base_voice_params_file")
    xvf_voice_params_file = LaunchConfiguration("xvf_voice_params_file")
    tts_backend = LaunchConfiguration("tts_backend")

    # ── Голос ─────────────────────────────────────────────────────────────────
    # autostart -- пробрасывается (default "false"): супервизор (группа
    # "voice") обычно владеет bring-up-ом; autostart:=true самоподнимает
    # без него, для standalone-тестирования.
    # params_file передан явно -- см. «ГРАБЛЯ» в шапке файла.
    legacy_voice = GroupAction(
        condition=IfCondition(
            PythonExpression(
                [
                    "'",
                    launch_voice,
                    "'.lower() in ('true', '1', 'yes') and '",
                    voice_profile,
                    "' == 'legacy'",
                ]
            )
        ),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_voice, "launch", "voice.launch.py")
                ),
                launch_arguments={
                    "params_file": voice_params_file,
                    "autostart": autostart,
                }.items(),
            ),
        ],
    )

    xvf_voice = GroupAction(
        condition=IfCondition(
            PythonExpression(
                [
                    "'",
                    launch_voice,
                    "'.lower() in ('true', '1', 'yes') and '",
                    voice_profile,
                    "' == 'xvf3800'",
                ]
            )
        ),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_voice, "launch", "xvf3800_voice.launch.py")
                ),
                launch_arguments={
                    "audio_params": xvf_audio_params_file,
                    "voice_params": xvf_base_voice_params_file,
                    "xvf_voice_params": xvf_voice_params_file,
                    "tts_backend": tts_backend,
                    "autostart": autostart,
                }.items(),
            ),
        ],
    )

    # ── Семантическая карта ──────────────────────────────────────────────────
    # autostart -- пробрасывается (default "false"), см. блок voice выше.
    # params_file передан явно -- см. «ГРАБЛЯ» в шапке файла.
    semantic_map = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_semantic_map, "launch", "semantic_map.launch.py")
                ),
                condition=IfCondition(launch_semantic_map),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "params_file": os.path.join(pkg_semantic_map, "config", "semantic_map.yaml"),
                    "autostart": autostart,
                }.items(),
            ),
        ],
    )

    # ── mission_control ──────────────────────────────────────────────────────
    # autostart -- пробрасывается (default "false"), см. блок voice выше.
    # params_file передан явно -- см. «ГРАБЛЯ» в шапке файла.
    mission = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_mission_control, "launch", "mission.launch.py")
                ),
                condition=IfCondition(launch_mission),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "params_file": os.path.join(pkg_mission_control, "config", "mission.yaml"),
                    "autostart": autostart,
                }.items(),
            ),
        ],
    )

    # ── лицо ─────────────────────────────────────────────────────────────────
    # Не lifecycle: HTTP-сервер + SVG, супервизор его не трогает. Kiosk
    # (Firefox на хосте Jetson) смотрит в http://127.0.0.1:8090.
    face = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(os.path.join(pkg_face, "launch", "face.launch.py")),
                condition=IfCondition(launch_face),
            ),
        ],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_launch_voice,
            declare_launch_semantic_map,
            declare_launch_mission,
            declare_launch_face,
            declare_autostart,
            declare_voice_params_file,
            declare_voice_profile,
            declare_xvf_audio_params_file,
            declare_xvf_base_voice_params_file,
            declare_xvf_voice_params_file,
            declare_tts_backend,
            legacy_voice,
            xvf_voice,
            semantic_map,
            mission,
            face,
        ]
    )
