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

    # mich1342/ros2_laser_scan_merger outputs PointCloud2.
    # NOTE: This specific merger is not fully TF-aware for input poses,
    # it uses manual offset parameters. We set them to match the URDF:
    # Left: Y = -0.258, Right: Y = +0.258
    scan_merger_node = Node(
        package="ros2_laser_scan_merger",
        executable="ros2_laser_scan_merger",
        name="scan_merger",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "scanTopic1": "/scan_left",
                "laser1XOff": 0.0,
                "laser1YOff": -0.258,
                "laser1ZOff": 0.0,
                "laser1Alpha": 0.0,
                "show1": True,
                "scanTopic2": "/scan_right",
                "laser2XOff": 0.0,
                "laser2YOff": 0.258,
                "laser2ZOff": 0.0,
                "laser2Alpha": 0.0,
                "show2": True,
                "pointCloudTopic": "/scan_merged_cloud",
                "pointCloutFrameId": merge_frame,
            }
        ],
    )

    # Convert the merged PointCloud2 back into a unified LaserScan on /scan
    pc_to_laserscan_node = Node(
        package="pointcloud_to_laserscan",
        executable="pointcloud_to_laserscan_node",
        name="pc_to_laserscan",
        output="screen",
        remappings=[
            ("cloud_in", "/scan_merged_cloud"),
            ("scan", "/scan"),
        ],
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "target_frame": merge_frame,
                "transform_tolerance": 0.01,
                "min_height": -0.5,
                "max_height": 1.5,
                "angle_min": -3.14159,
                "angle_max": 3.14159,
                "angle_increment": 0.005,
                "scan_time": 0.1,
                "range_min": 0.1,
                "range_max": 12.0,
                "use_inf": True,
                "inf_epsilon": 1.0,
            }
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
            scan_merger_node,
            pc_to_laserscan_node,
        ]
    )
