# =========================================================================
#  perception.launch.py — единая точка входа для сенсорики.
#
#  real_lidars:=true   реальное железо: sensors.launch.py
#                      (2x sllidar C1 -> laser_sector_blanker -> dual_laser_merger
#                       с калибровкой laser_2_* offsets)
#  real_lidars:=false  Gazebo сам публикует /scan_left и /scan_right,
#                      поднимается только dual_laser_merger (без калибровки)
#
#  В обоих случаях на выходе: /scan (LaserScan в merge_frame).
#  Соноры (sensor_msgs/Range на sonar/range/<frame_id>) — launch_sonar:=true,
#  в симуляции их публикуют плагины Gazebo, поэтому там launch_sonar:=false.
# =========================================================================

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Launch the perception stack (lidars + scan merger + sonars)."""
    pkg_bringup = get_package_share_directory("guide_robot_bringup")

    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_real_lidars = DeclareLaunchArgument(
        "real_lidars",
        default_value="true",
        description="true — драйверы RPLIDAR C1 + бланкеры + мерджер (железо); "
        "false — только мерджер поверх /scan_left, /scan_right из Gazebo",
    )
    declare_launch_sonar = DeclareLaunchArgument(
        "launch_sonar",
        default_value="true",
        description="Launch sonar range node (только железо)",
    )
    declare_merge_frame = DeclareLaunchArgument(
        "merge_frame",
        default_value="base_footprint",
        description="Target TF frame for the merged scan",
    )
    declare_left_port = DeclareLaunchArgument(
        "left_port", default_value="/dev/tty_lidar_left", description="Serial port, LEFT lidar"
    )
    declare_right_port = DeclareLaunchArgument(
        "right_port", default_value="/dev/tty_lidar_right", description="Serial port, RIGHT lidar"
    )

    use_sim_time = LaunchConfiguration("use_sim_time")
    real_lidars = LaunchConfiguration("real_lidars")
    launch_sonar = LaunchConfiguration("launch_sonar")
    merge_frame = LaunchConfiguration("merge_frame")
    left_port = LaunchConfiguration("left_port")
    right_port = LaunchConfiguration("right_port")

    # ── Железо: драйверы + бланкеры + калиброванный мерджер ───────────────────
    lidars_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_bringup, "launch", "lidars.launch.py")),
        condition=IfCondition(real_lidars),
        launch_arguments={
            "use_sim_time": use_sim_time,
            "merge_frame": merge_frame,
            "left_port": left_port,
            "right_port": right_port,
        }.items(),
    )

    # ── Симуляция: только мерджер, без калибровочных offset'ов ───────────────
    #  Gazebo уже отдаёт /scan_left и /scan_right в правильных TF-фреймах,
    #  self-hit'ов мачт нет — бланкеры не нужны.
    sim_merger = Node(
        package="dual_laser_merger",
        executable="dual_laser_merger_node",
        name="dual_laser_merger",
        output="screen",
        condition=UnlessCondition(real_lidars),
        remappings=[
            # Топик публикатора создаётся до чтения merged_scan_topic —
            # ремап, а не параметр.
            ("merged", "/scan"),
            ("merged_cloud", "/scan_merged_cloud"),
        ],
        parameters=[
            {"use_sim_time": use_sim_time},
            {
                "laser_1_topic": "/scan_left",
                "laser_2_topic": "/scan_right",
                "target_frame": merge_frame,
                "tolerance": 0.05,
                "queue_size": 10,
                "angle_increment": 0.005,
                "scan_time": 0.1,
                "range_min": 0.1,
                "range_max": 12.0,
                "min_height": -0.5,
                "max_height": 1.5,
                "angle_min": -3.141592654,
                "angle_max": 3.141592654,
                "use_inf": True,
                "inf_epsilon": 1.0,
                "enable_calibration": False,
                "enable_average_filter": False,
                "enable_shadow_filter": False,
            },
        ],
    )

    # ── Соноры ────────────────────────────────────────────────────────────────
    sonar_node = Node(
        package="guide_robot_sonar",
        executable="sonar_node_mult.py",
        name="sonar_node",
        output="screen",
        condition=IfCondition(launch_sonar),
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "publish_inf_as_out_of_range": True,
            }
        ],
    )

    return LaunchDescription(
        [
            declare_use_sim_time,
            declare_real_lidars,
            declare_launch_sonar,
            declare_merge_frame,
            declare_left_port,
            declare_right_port,
            lidars_launch,
            sim_merger,
            sonar_node,
        ]
    )
