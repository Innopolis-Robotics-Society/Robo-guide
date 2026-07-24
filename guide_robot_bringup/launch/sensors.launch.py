"""Launch two RPLIDAR C1 sensors via sllidar_ros2 and merge their scans.

Topology:
  /scan_left   (laser_frame_left)  ──┬
                                      ├─► ros2_laser_scan_merger ─► /scan
  /scan_right  (laser_frame_right) ──┘

RPLIDAR C1 specs:
  baudrate  : 460800
  scan rate : 10 Hz (fixed)
  range     : up to 12 m
  scan_mode : leave empty to use C1 default

Merger output:
  /scan      — merged LaserScan in base_footprint frame (fed to Nav2 / SLAM)
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, TimerAction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Generate launch description to launch two RPLIDAR C1 sensors and merge their scans."""
    # ── Launch arguments ──────────────────────────────────────────────────────
    declare_left_port = DeclareLaunchArgument(
        "left_port",
        default_value="/dev/tty_lidar_left",
        description="Serial port for the LEFT lidar",
    )
    declare_right_port = DeclareLaunchArgument(
        "right_port",
        default_value="/dev/tty_lidar_right",
        description="Serial port for the RIGHT lidar",
    )
    declare_baudrate = DeclareLaunchArgument(
        "baudrate",
        default_value="460800",
        description="Serial baudrate — 460800 for RPLIDAR C1",
    )
    declare_use_sim_time = DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use simulation clock if true",
    )
    declare_merge_frame = DeclareLaunchArgument(
        "merge_frame",
        default_value="base_footprint",
        description="Target TF frame for the merged scan (must be in TF tree)",
    )
    declare_lidar_delay = DeclareLaunchArgument(
        "lidar_start_delay",
        default_value="5.0",
        description="Seconds to wait before starting the RIGHT lidar (avoids power surge)",
    )

    left_port = LaunchConfiguration("left_port")
    right_port = LaunchConfiguration("right_port")
    baudrate = LaunchConfiguration("baudrate")
    use_sim_time = LaunchConfiguration("use_sim_time")
    merge_frame = LaunchConfiguration("merge_frame")
    lidar_delay = LaunchConfiguration("lidar_start_delay")

    # ── LEFT lidar ─────────────────────────────────────────────────────────────
    # Publishes to: /scan_left
    # frame_id must match the URDF link: laser_frame_left
    lidar_left_node = Node(
        package="sllidar_ros2",
        executable="sllidar_node",
        name="sllidar_left",
        output="screen",
        parameters=[
            {
                "serial_port": left_port,
                "serial_baudrate": baudrate,
                "frame_id": "laser_frame_left",
                "inverted": True,  # Fixes the flipped Y-axis
                "angle_compensate": True,
                "use_sim_time": use_sim_time,
            }
        ],
        remappings=[
            ("/scan", "/scan_left"),
        ],
    )

    lidar_right_node = TimerAction(
        period=lidar_delay,
        actions=[
            Node(
                package="sllidar_ros2",
                executable="sllidar_node",
                name="sllidar_right",
                output="screen",
                parameters=[
                    {
                        "serial_port": right_port,
                        "serial_baudrate": baudrate,
                        "frame_id": "laser_frame_right",
                        "inverted": True,  # Fixes the flipped Y-axis
                        "angle_compensate": True,
                        "use_sim_time": use_sim_time,
                    }
                ],
                remappings=[
                    ("/scan", "/scan_right"),
                ],
            )
        ],
    )

    # Merges /scan_left and /scan_right using TF into a single LaserScan on /scan
    merger_node = Node(
        package="dual_laser_merger",
        executable="dual_laser_merger_node",
        name="dual_laser_merger",
        output="screen",
        remappings=[
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

    return LaunchDescription(
        [
            declare_left_port,
            declare_right_port,
            declare_baudrate,
            declare_use_sim_time,
            declare_merge_frame,
            declare_lidar_delay,
            lidar_left_node,
            lidar_right_node,  # delayed via TimerAction
            merger_node,
        ]
    )
