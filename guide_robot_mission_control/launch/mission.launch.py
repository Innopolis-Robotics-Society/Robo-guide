"""Запуск стека mission_control: presence_monitor + narration_server + mission_fsm.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию -- та же причина,
что и в guide_robot_voice/guide_robot_llm/guide_robot_semantic_map: порядок
переходов должен быть управляемым извне (супервизором или вручную через
`ros2 lifecycle set`), а не зашитым в launch. guide_robot_mission_control,
как и voice/llm/semantic_map, НЕ зарегистрирован в guide_robot_supervisor/
config/supervisor.yaml -- тот файл жёстко про safety-critical нав-стек
(safety/localization/navigation), а голосовой/mission/llm-слой поверх него --
служебный слой, которым supervisor сознательно не владеет (design §10 говорит
"регистрация в супервизоре", но реальная конвенция репозитория для этого
слоя -- собственный lifecycle_manager в своём launch-файле, не запись в
центральный supervisor.yaml; см. те же докстринги в voice/llm/semantic_map).

autostart:=true поднимает всё сразу через lifecycle_manager -- удобно для
разработки, не рабочий режим на роботе.

use_container:=true (дефолт, design §1) -- mission_fsm и narration_server
поднимаются ОДНИМ процессом (`mission_container`) на общем
MultiThreadedExecutor: убирает межпроцессный DDS-скачок на пути
barge-in -> пауза нарратива. use_container:=false поднимает их как два
отдельных процесса -- удобнее для отладки (падение/лог одного узла не тянет
за собой другой). presence_monitor всегда отдельным процессом -- он не в
горячем пути прерывания (см. докстринг mission_container.py).

Порядок bring-up (design §10, критичен при autostart:=true):
  presence_monitor -> narration_server -> mission_fsm
Тот же порядок узлов lifecycle_manager видит независимо от use_container --
имена узлов внутри mission_container фиксированы в их __init__ ("mission_fsm",
"narration_server"), совпадают с именами отдельных executables.
"""

from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription

# design §10: presence_monitor -> narration_server -> mission_fsm. Порядок
# существен только для lifecycle-переходов при autostart:=true (mission_fsm
# на activate начинает публиковать /mission/state и принимать RunTour --
# должен подняться последним, когда его клиенты уже готовы отвечать).
BRINGUP_ORDER = ["presence_monitor", "narration_server", "mission_fsm"]


def generate_launch_description() -> LaunchDescription:
    """Собрать описание запуска."""
    params = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    use_container = LaunchConfiguration("use_container")
    autostart = LaunchConfiguration("autostart")
    log_level = LaunchConfiguration("log_level")

    arguments = [
        DeclareLaunchArgument(
            "params_file",
            default_value=PathJoinSubstitution(
                [FindPackageShare("guide_robot_mission_control"), "config", "mission.yaml"]
            ),
        ),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument(
            "use_container",
            default_value="true",
            description="mission_fsm+narration_server одним процессом (design §1)",
        ),
        DeclareLaunchArgument("autostart", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]

    node_params = [ParameterFile(params, allow_substs=True), {"use_sim_time": use_sim_time}]

    presence_monitor = Node(
        package="guide_robot_mission_control",
        executable="presence_monitor",
        name="presence_monitor",
        output="screen",
        parameters=node_params,
        arguments=["--ros-args", "--log-level", log_level],
    )

    container = Node(
        package="guide_robot_mission_control",
        executable="mission_container",
        # БЕЗ name=: launch_ros реализует name= через `--ros-args -r
        # __node:=<name>` -- глобальный remap, который применился бы
        # СРАЗУ к обоим узлам процесса (MissionFsmNode и NarrationServerNode
        # оба хардкодят своё имя в super().__init__(), но __node-remap
        # переопределяет любое переданное имя на уровне rcl, а не только
        # дефолтное). С name="mission_container" оба узла регистрировались
        # в графе как /mission_container -- lifecycle_manager_mission
        # висел вечно на narration_server/get_state, которого не существовало
        # (воспроизведено и подтверждено вручную).
        output="screen",
        condition=IfCondition(use_container),
        parameters=node_params,
        arguments=["--ros-args", "--log-level", log_level],
    )

    fsm_and_narration_separate = [
        Node(
            package="guide_robot_mission_control",
            executable=name,
            name=name,
            output="screen",
            condition=UnlessCondition(use_container),
            parameters=node_params,
            arguments=["--ros-args", "--log-level", log_level],
        )
        for name in ("narration_server", "mission_fsm")
    ]

    manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_mission",
        output="screen",
        condition=IfCondition(autostart),
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "autostart": True,
                "node_names": BRINGUP_ORDER,
                # 0.0 -- см. guide_robot_voice/launch/voice.launch.py: наши
                # ноды -- обычные rclpy.lifecycle.LifecycleNode, bond не
                # создают, lifecycle_manager без этого валит bringup по
                # bond_timeout, так и не дождавшись heartbeat.
                "bond_timeout": 0.0,
            }
        ],
    )

    return LaunchDescription(
        [
            *arguments,
            GroupAction([presence_monitor, container, *fsm_and_narration_separate, manager]),
        ]
    )
