# =========================================================================
#  nav_stack.launch.py — единая точка входа для навигации.
#  Идентичен для симуляции и железа, различие только в аргументах.
#
#  nav:=true,  slam:=true, slam_backend:=slam_toolbox
#                            -> SLAM Toolbox + Nav2 + collision_monitor
#  nav:=true,  slam:=true, slam_backend:=cartographer
#                            -> Cartographer + Nav2 + collision_monitor
#  nav:=true,  slam:=false  -> navigation.launch.py
#                              (map_server + AMCL + Nav2 + collision_monitor)
#  nav:=false, slam:=true   -> только выбранный SLAM backend
#  nav:=false, slam:=false  -> ничего не поднимается
#
#  Супервизор поднимается отдельно (launch_supervisor:=true) и берёт
#  backend-specific SLAM config или supervisor.yaml для AMCL.
#
#  Имя файла — nav_stack, а не navigation, чтобы не путать с
#  guide_robot_navigation/launch/navigation.launch.py.
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression


def generate_launch_description():
    """Launch the navigation stack (SLAM or AMCL + Nav2 + supervisor)."""
    pkg_navigation = get_package_share_directory("guide_robot_navigation")
    pkg_supervisor = get_package_share_directory("guide_robot_supervisor")
    pkg_slam_toolbox = get_package_share_directory("slam_toolbox")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_nav = DeclareLaunchArgument(
        "nav",
        default_value="true",
        description="Launch Nav2 (planner, controller, behaviors, collision_monitor)",
    )
    declare_slam = DeclareLaunchArgument(
        "slam",
        default_value="false",
        description="true — строить карту; false — AMCL по готовой карте из map",
    )
    declare_slam_backend = DeclareLaunchArgument(
        "slam_backend",
        default_value="slam_toolbox",
        choices=["slam_toolbox", "cartographer"],
        description="SLAM backend: slam_toolbox or cartographer",
    )
    declare_map = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg_navigation, "map", "lab_map.yaml"),
        description="Готовая карта, используется только при slam:=false",
    )
    declare_nav_params = DeclareLaunchArgument(
        "nav_params_file",
        default_value=os.path.join(pkg_navigation, "config", "first_iter_nav2.yaml"),
        description="Full path to Nav2 parameters file",
    )
    declare_slam_params = DeclareLaunchArgument(
        "slam_params_file",
        default_value=os.path.join(pkg_navigation, "config", "mapper_params_online_async.yaml"),
        description="Full path to SLAM Toolbox parameters file",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav",
        default_value="false",
        description="Autostart Nav2 lifecycle nodes without supervisor orchestration",
    )
    declare_launch_supervisor = DeclareLaunchArgument(
        "launch_supervisor", default_value="true", description="Launch guide_robot_supervisor"
    )
    declare_autostart_supervisor = DeclareLaunchArgument(
        "autostart_supervisor",
        default_value="true",
        description="Let the supervisor bring the lifecycle groups up on its own; "
        "false keeps it idle in INIT until /supervisor/bringup is called",
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    nav = LaunchConfiguration("nav")
    slam = LaunchConfiguration("slam")
    slam_backend = LaunchConfiguration("slam_backend")
    map_yaml_file = LaunchConfiguration("map")
    nav_params_file = LaunchConfiguration("nav_params_file")
    slam_params_file = LaunchConfiguration("slam_params_file")
    autostart_nav = LaunchConfiguration("autostart_nav")
    launch_supervisor = LaunchConfiguration("launch_supervisor")
    autostart_supervisor = LaunchConfiguration("autostart_supervisor")

    # Backend conditions also include slam:=true.  If slam:=false, backend is
    # deliberately ignored and the AMCL branch below is selected.
    is_slam_toolbox = PythonExpression(
        [
            "'",
            slam,
            "'.lower() == 'true' and '",
            slam_backend,
            "' == 'slam_toolbox'",
        ]
    )
    is_cartographer = PythonExpression(
        [
            "'",
            slam,
            "'.lower() == 'true' and '",
            slam_backend,
            "' == 'cartographer'",
        ]
    )
    should_launch_supervisor = PythonExpression(
        [
            "'",
            launch_supervisor,
            "'.lower() == 'true' and '",
            nav,
            "'.lower() == 'true'",
        ]
    )

    # ── Nav2 (+ локализация) ──────────────────────────────────────────────────
    nav_group = GroupAction(
        condition=IfCondition(nav),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_navigation, "launch", "slam_navigation.launch.py")
                ),
                condition=IfCondition(is_slam_toolbox),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart_nav": autostart_nav,
                    "slam_params_file": slam_params_file,
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(
                        pkg_navigation, "launch", "cartographer_navigation.launch.py"
                    )
                ),
                condition=IfCondition(is_cartographer),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "nav": "true",
                    "autostart_nav": autostart_nav,
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_navigation, "launch", "navigation.launch.py")
                ),
                condition=UnlessCondition(slam),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart_nav": autostart_nav,
                    "map": map_yaml_file,
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
        ],
    )

    # ── Только картирование, без Nav2 ─────────────────────────────────────────
    slam_only_group = GroupAction(
        condition=UnlessCondition(nav),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_slam_toolbox, "launch", "online_async_launch.py")
                ),
                condition=IfCondition(is_slam_toolbox),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "slam_params_file": slam_params_file,
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(
                        pkg_navigation, "launch", "cartographer_navigation.launch.py"
                    )
                ),
                condition=IfCondition(is_cartographer),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "nav": "false",
                    "autostart_nav": "false",
                    "nav2_params_file": nav_params_file,
                }.items(),
            ),
        ],
    )

    # ── Супервизор ────────────────────────────────────────────────────────────
    supervisor_group = GroupAction(
        # A supervisor only manages Nav2 lifecycle groups.  Mapping-only mode
        # intentionally has neither those managers nor a supervisor waiting
        # for them.
        condition=IfCondition(should_launch_supervisor),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_supervisor, "launch", "supervisor.launch.py")
                ),
                condition=IfCondition(is_slam_toolbox),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart_supervisor": autostart_supervisor,
                    "config_file": os.path.join(
                        pkg_supervisor, "config", "supervisor_slam.yaml"
                    ),
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_supervisor, "launch", "supervisor.launch.py")
                ),
                condition=IfCondition(is_cartographer),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart_supervisor": autostart_supervisor,
                    "config_file": os.path.join(
                        pkg_supervisor, "config", "supervisor_cartographer.yaml"
                    ),
                }.items(),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(pkg_supervisor, "launch", "supervisor.launch.py")
                ),
                condition=UnlessCondition(slam),
                launch_arguments={
                    "use_sim_time": use_sim_time,
                    "autostart_supervisor": autostart_supervisor,
                    "config_file": os.path.join(pkg_supervisor, "config", "supervisor.yaml"),
                }.items(),
            ),
        ],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_nav,
            declare_slam,
            declare_slam_backend,
            declare_map,
            declare_nav_params,
            declare_slam_params,
            declare_autostart_nav,
            declare_launch_supervisor,
            declare_autostart_supervisor,
            nav_group,
            slam_only_group,
            supervisor_group,
        ]
    )
