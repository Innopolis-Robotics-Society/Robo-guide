# =========================================================================
#  high_level_stack.launch.py — единая точка входа для стека экскурсий.
#
#  Аудио и высокоуровневый служебный слой поверх нав-стека:
#    guide_robot_voice          — микрофон/динамик, ASR/VAD/wakeword, TTS
#    guide_robot_semantic_map   — route_server + content/location/route_planner
#    guide_robot_mission_control — mission_fsm + narration_server + presence_monitor
#
#  Отдельно от nav_stack.launch.py (там только safety/localization/
#  navigation/супервизор) -- собирается по образцу того же файла, но
#  сознательно не включается из него: nav-стек обязан подниматься и без
#  экскурсионного слоя (например, для чистого картирования/локализации).
#
#  Каждая из трёх groups в guide_robot_supervisor (voice/semantic_map/
#  mission, requires: [navigation, voice, semantic_map] у mission) ждёт,
#  что её lifecycle_manager уже существует как процесс -- этот launch-файл
#  их поднимает с autostart:=false, bring-up делает супервизор (см.
#  guide_robot_supervisor/config/supervisor.yaml).
#
#  Usage:
#    ros2 launch guide_robot_bringup high_level_stack.launch.py
#    ros2 launch guide_robot_bringup high_level_stack.launch.py launch_voice:=false
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
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """Launch the tour stack (voice + semantic_map + mission_control)."""
    pkg_voice = get_package_share_directory("guide_robot_voice")
    pkg_semantic_map = get_package_share_directory("guide_robot_semantic_map")
    pkg_mission_control = get_package_share_directory("guide_robot_mission_control")

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

    use_sim_time = LaunchConfiguration("use_sim_time")
    launch_voice = LaunchConfiguration("launch_voice")
    launch_semantic_map = LaunchConfiguration("launch_semantic_map")
    launch_mission = LaunchConfiguration("launch_mission")

    # ── Голос ─────────────────────────────────────────────────────────────────
    # autostart:=false -- супервизор (группа "voice") владеет bring-up-ом.
    # params_file передан явно -- см. «ГРАБЛЯ» в шапке файла.
    voice = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_voice, "launch", "voice.launch.py")
                ),
                condition=IfCondition(launch_voice),
                launch_arguments={
                    "params_file": os.path.join(pkg_voice, "config", "voice.yaml"),
                    "autostart": "false",
                }.items(),
            ),
        ],
    )

    # ── Семантическая карта ──────────────────────────────────────────────────
    # autostart:=false -- супервизор (группа "semantic_map") владеет bring-up-ом.
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
                    "params_file": os.path.join(
                        pkg_semantic_map, "config", "semantic_map.yaml"
                    ),
                    "autostart": "false",
                }.items(),
            ),
        ],
    )

    # ── mission_control ──────────────────────────────────────────────────────
    # autostart:=false -- супервизор (группа "mission") владеет bring-up-ом.
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
                    "params_file": os.path.join(
                        pkg_mission_control, "config", "mission.yaml"
                    ),
                    "autostart": "false",
                }.items(),
            ),
        ],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_launch_voice,
            declare_launch_semantic_map,
            declare_launch_mission,
            voice,
            semantic_map,
            mission,
        ]
    )
