import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    """Launch AMCL localization against a saved map plus the common Nav2 stack."""
    pkg = get_package_share_directory("guide_robot_navigation")
    nav2_launch_dir = os.path.join(get_package_share_directory("nav2_bringup"), "launch")

    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart_nav = LaunchConfiguration("autostart_nav")
    map_yaml = LaunchConfiguration("map")
    nav2_params = LaunchConfiguration("nav2_params_file")
    keepout_mask_file = LaunchConfiguration("keepout_mask_file")

    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use simulation clock",
    )
    declare_autostart_nav = DeclareLaunchArgument(
        "autostart_nav",
        default_value="false",
        description="Autostart lifecycle nodes",
    )
    declare_map = DeclareLaunchArgument(
        "map",
        default_value=os.path.join(pkg, "map", "simple.yaml"),
        description="Full path to map yaml",
    )
    declare_nav2_params = DeclareLaunchArgument(
        "nav2_params_file",
        default_value=os.path.join(pkg, "config", "first_iter_nav2.yaml"),
        description="Nav2 parameters file",
    )
    declare_keepout_mask_file = DeclareLaunchArgument(
        "keepout_mask_file",
        default_value="",
        description="Full path to keepout mask yaml (must match the active `map`'s "
        "origin/resolution). Empty -- keepout filter stays off (no matching mask "
        "for this map yet), costmaps behave exactly as before.",
    )

    # map_server + amcl from nav2_bringup, non-composed to match common.
    localization = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(nav2_launch_dir, "localization_launch.py")),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart": autostart_nav,
            "params_file": nav2_params,
            "map": map_yaml,
            "use_composition": "False",
        }.items(),
    )

    common = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg, "launch", "common.launch.py")),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "autostart_nav": autostart_nav,
            "nav2_params_file": nav2_params,
        }.items(),
    )

    # Keepout-зоны (Nav2 costmap filters), только на этом (AMCL) пути и только
    # когда для активной `map` подготовлена своя маска -- см. `first_iter_nav2.
    # yaml.in` (keepout_filter в обоих костмапах, включается по тому же
    # topic/filter_info_topic, что публикуют эти два узла).
    filter_mask_server = Node(
        package="nav2_map_server",
        executable="map_server",
        name="filter_mask_server",
        output="screen",
        parameters=[
            nav2_params,
            {"use_sim_time": use_sim_time, "yaml_filename": keepout_mask_file},
        ],
    )
    costmap_filter_info_server = Node(
        package="nav2_map_server",
        executable="costmap_filter_info_server",
        name="costmap_filter_info_server",
        output="screen",
        parameters=[nav2_params, {"use_sim_time": use_sim_time}],
    )
    # Отдельный менеджер (по образцу lifecycle_manager_safety в common.
    # launch.py) -- filter_mask_server/costmap_filter_info_server не входят
    # ни в lifecycle_manager_localization (хардкод в nav2_bringup, не
    # дописать), ни в lifecycle_manager_navigation (тем более).
    lifecycle_costmap_filters = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_costmap_filters",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "autostart": autostart_nav,
                "node_names": ["filter_mask_server", "costmap_filter_info_server"],
                "bond_timeout": 4.0,
            }
        ],
    )
    keepout_group = GroupAction(
        condition=IfCondition(PythonExpression(['"', keepout_mask_file, '" != ""'])),
        actions=[filter_mask_server, costmap_filter_info_server, lifecycle_costmap_filters],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_autostart_nav,
            declare_map,
            declare_nav2_params,
            declare_keepout_mask_file,
            localization,
            common,
            keepout_group,
        ]
    )
