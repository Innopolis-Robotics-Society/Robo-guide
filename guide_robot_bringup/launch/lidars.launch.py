"""Launch two RPLIDAR C1 sensors via sllidar_ros2 and merge their scans.

Topology:
  /scan_left   -> laser_sector_blanker -> /scan_left_filtered  --┐
                                                                 ├-> scan_merger -> /scan
  /scan_right  -> laser_sector_blanker -> /scan_right_filtered --┘

RPLIDAR C1 specs:
  baudrate  : 460800
  scan rate : 10 Hz (fixed)
  range     : up to 12 m
  scan_mode : leave empty to use C1 default

Each lidar sees its own mount / the other lidar's mount at a fixed bearing in
its own frame on every scan (self-hit, not a real obstacle). laser_sector_blanker
blanks that bearing out of /scan_left and /scan_right before they reach the
merger — the merger's output binning only clips the *merged* output's ends,
it can't mask a wedge inside one lidar's field of view.
left/right_blind_sectors_deg are hardcoded below (not launch args) — a one-time
per-robot fit found with laser_blind_sector_finder (run it against /scan_left
and /scan_right separately, see that node's docstring for usage).

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

    # Own-frame bearings (degrees, raw /scan_left, /scan_right angle
    # convention) where each lidar sees its own mount bracket / the other
    # lidar's mount. laser_blind_sector_finder's auto-detected sub-clusters
    # (movement-confirmed real self-hits) matched this range almost exactly
    # but were full of small gaps at the noisy, grazing-angle edges of the
    # bracket - one clean contiguous sector covers the whole physical
    # obstruction instead of leaking points through those gaps.
    #
    # Raw angle 0 in these topics points robot-*backward*, not forward (the
    # upside-down + front-to-back mount flip - see laser_joint_left/right in
    # guide_robot.urdf.xacro): raw = forward_relative_angle + 180 (mod 360).
    # So "the mount sits ~78-172 deg left of forward" (as measured directly
    # on the robot) becomes this raw range.
    #
    # hardcoded here rather than exposed as launch args because, like the
    # merger's laser_*_offset calibration below, this is a one-time
    # per-robot fit, not something you'd want to override at launch time.
    left_blind_sectors_deg = "-105.0,-8.0"
    # right is the mirror image (mount ~78-172 deg right of forward), and
    # already extends to 103.89 instead of 102 to also cover the confirmed
    # 0.516m (= 2x lidar_y_offset) sighting of the LEFT lidar's mount across
    # the bar - that measured extra reach is wider than the near-edge bump
    # below, so nothing to widen here.
    right_blind_sectors_deg = "8.0,105.0"

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
                # Reverses the ranges-array order relative to the fixed
                # angle array (sllidar_node.cpp), which mirrors the arc
                # left/right. False here matches the real mount.
                "inverted": False,
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
                        # See sllidar_left above.
                        "inverted": False,
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

    # Blank out each lidar's self-hit sector(s) before they reach the merger.
    left_blanker_node = Node(
        package="guide_robot_bringup",
        executable="laser_sector_blanker",
        name="laser_sector_blanker_left",
        output="screen",
        parameters=[
            {
                "input_topic": "/scan_left",
                "output_topic": "/scan_left_filtered",
                "blind_sectors_deg": left_blind_sectors_deg,
                "use_sim_time": use_sim_time,
            }
        ],
    )
    right_blanker_node = Node(
        package="guide_robot_bringup",
        executable="laser_sector_blanker",
        name="laser_sector_blanker_right",
        output="screen",
        parameters=[
            {
                "input_topic": "/scan_right",
                "output_topic": "/scan_right_filtered",
                "blind_sectors_deg": right_blind_sectors_deg,
                "use_sim_time": use_sim_time,
            }
        ],
    )

    # Merges /scan_left_filtered + /scan_right_filtered into /scan with motion
    # compensation: each scan is deskewed to a common instant (midpoint of the
    # two sweep centers) through the odom->base_footprint TF before binning.
    # This replaced dual_laser_merger (2026-08-11): ApproximateTime pairing
    # there left up to ~50 ms of inter-scan desync (a visible rotation between
    # the two halves of /scan on turns) and added ~100 ms of partner-wait
    # latency. Calibration offsets are the same numbers the old merger got
    # via laser_*_offset (ICP fit against a shared wall).
    merger_node = Node(
        package="guide_robot_bringup",
        executable="scan_merger",
        name="scan_merger",
        output="screen",
        parameters=[
            {
                "use_sim_time": use_sim_time,
                "laser_1_topic": "/scan_left_filtered",
                "laser_2_topic": "/scan_right_filtered",
                "output_topic": "/scan",
                "target_frame": merge_frame,
                "laser_1_x_offset": 0.0,
                "laser_1_y_offset": 0.0,
                "laser_1_yaw_offset": 0.0,
                "laser_2_x_offset": 0.0,
                "laser_2_y_offset": -0.016,
                "laser_2_yaw_offset": 0.028,
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
            left_blanker_node,
            right_blanker_node,
            merger_node,
        ]
    )
