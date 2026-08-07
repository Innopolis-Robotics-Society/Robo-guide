"""Запуск стека mission_control: presence_monitor + narration_server + mission_fsm.

Ноды lifecycle и НЕ поднимаются автоматически по умолчанию -- порядок
переходов должен быть управляемым извне (супервизором или вручную через
`ros2 lifecycle set`), а не зашитым в launch.

В отличие от исходного решения этого файла, этот стек ЗАРЕГИСТРИРОВАН в
guide_robot_supervisor как группа `mission` (config/supervisor.yaml,
config/supervisor_slam.yaml, requires: [navigation, voice, semantic_map],
optional: true) -- по прямому запросу, design §10 ("регистрация в
супервизоре") реализован буквально. `voice`/`semantic_map` зарегистрированы
там же тем же способом (см. их launch-файлы) -- narration_server
(mission_container) зовёт Say (voice) и ~/get_exhibit_content (semantic_map),
должны быть подняты раньше mission. `optional: true` на все три, чтобы
отказ любой из групп не переводил ВЕСЬ supervisor в FAULT и не блокировал
уже поднятый safety/localization/navigation.

Сам процесс этого пакета поднимается ОТДЕЛЬНЫМ launch-файлом
guide_robot_bringup/launch/high_level_stack.launch.py (вместе с voice и
semantic_map) -- не через nav_stack.launch.py (там только safety/
localization/navigation/супервизор).

Как и `guide_robot_navigation/launch/common.launch.py` (`lifecycle_manager_safety`),
`lifecycle_manager_mission` запускается ВСЕГДА (не под условием) -- супервизор
дёргает его `~/manage_nodes` сервис напрямую, и сервис должен существовать
независимо от того, кто (supervisor или разработчик руками) решит вызвать
STARTUP. Параметр `autostart` пробрасывается в сам lifecycle_manager как есть
(default "false" -- контракт супервизора: "Every lifecycle_manager referenced
here MUST have autostart: false", guide_robot_supervisor/config/supervisor.yaml:4);
autostart:=true остаётся для разработки без супервизора -- поднимает всё сразу
самостоятельно.

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
        # БЕЗ condition=IfCondition(autostart): супервизор дёргает
        # ~/manage_nodes этого менеджера напрямую (guide_robot_supervisor/
        # config/supervisor.yaml, группа "mission"), сервис обязан
        # существовать вне зависимости от того, кто инициирует STARTUP --
        # см. lifecycle_manager_safety в
        # guide_robot_navigation/launch/common.launch.py, тот же паттерн.
        parameters=[
            {
                "use_sim_time": use_sim_time,
                # Пробрасываем launch-arg как есть (default "false") --
                # контракт супервизора: "Every lifecycle_manager referenced
                # here MUST have autostart: false"
                # (guide_robot_supervisor/config/supervisor.yaml:4).
                # autostart:=true -- самостоятельный подъём для разработки
                # без супервизора.
                "autostart": autostart,
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
