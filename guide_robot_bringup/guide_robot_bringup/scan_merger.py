#!/usr/bin/env python3
"""Merge two LaserScans into one /scan with motion compensation (deskew).

Replaces the external dual_laser_merger on the real robot. That merger pairs
scans with message_filters.ApproximateTime and transforms each cloud by the
STATIC TF only, so on a rotating robot the two halves of /scan disagree by
|stamp_L - stamp_R| * omega (up to ~50 ms of pairing slop plus the half-sweep
offset — ~5 degrees at omega=1 rad/s), and the pairing wait adds ~100 ms of
latency that anti-correlates with the desync, so neither can be tuned away.

Here each scan is deskewed instead: points go laser frame -> target frame
(static TF + per-lidar planar calibration) -> base pose at a COMMON instant
via the odom->base_footprint TF (which interpolates at the 50 Hz odom rate).
A scan is represented by its sweep-center instant (stamp + scan_time/2,
matching how sllidar timestamps the sweep start); the common instant is the
midpoint of the two centers. Because desync is compensated rather than
avoided, pairing is nearest-neighbour on arrival — no waiting for the
partner, so the merger no longer adds a scan period of latency.

Pairing uses WALL-CLOCK age of the partner, not |stamp_L - stamp_R|: two
free-running 10 Hz C1s routinely sit 50-140 ms apart in stamp (half a period
plus jitter), and that gap is exactly what deskew removes. A stamp-diff
threshold of 0.1 s false-alarmed "lidar may be down" on a healthy pair.
Output is one merge per unique pair (triggered by the later of the two
stamps), ~10 Hz, not 20.

Failure semantics: if one lidar dies, its partner's wall-clock age exceeds
pair_tolerance and /scan goes silent, which downstream timeouts already
handle. The intentional 5 s right-lidar start delay is silent (no partner
yet), not a WARN.

All math lives in scan_merge_math (ROS-free, unit-tested); this file is the
rclpy/tf2 plumbing only.
"""

import math
import time

import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from tf2_ros import Buffer, TransformException, TransformListener

from guide_robot_bringup import scan_merge_math as smm


def _stamp_ns(stamp):
    """Convert a builtin_interfaces/Time to integer nanoseconds."""
    return stamp.sec * 1_000_000_000 + stamp.nanosec


def _tf_matrix(transform_stamped):
    """Convert a TransformStamped to a 4x4 homogeneous matrix."""
    t = transform_stamped.transform.translation
    q = transform_stamped.transform.rotation
    return smm.make_transform((t.x, t.y, t.z), (q.x, q.y, q.z, q.w))


class ScanMerger(Node):
    """Merge two deskewed LaserScans into a single LaserScan in the target frame."""

    def __init__(self):
        """Declare parameters, set up TF and the subscription/publisher pair."""
        super().__init__("scan_merger")

        self.declare_parameter("laser_1_topic", "/scan_left_filtered")
        self.declare_parameter("laser_2_topic", "/scan_right_filtered")
        self.declare_parameter("output_topic", "/scan")
        self.declare_parameter("target_frame", "base_footprint")
        self.declare_parameter("fixed_frame", "odom")
        # Max WALL-CLOCK age of the partner. Deskew absorbs stamp-phase
        # (routinely 50-140 ms on free-running C1s); this bound only trips
        # when one lidar actually stops delivering.
        self.declare_parameter("pair_tolerance", 0.25)
        self.declare_parameter("scan_time", 0.1)
        self.declare_parameter("tf_timeout", 0.05)
        self.declare_parameter("angle_min", -math.pi)
        self.declare_parameter("angle_max", math.pi)
        self.declare_parameter("angle_increment", 0.005)
        self.declare_parameter("range_min", 0.1)
        self.declare_parameter("range_max", 12.0)
        self.declare_parameter("use_inf", True)
        self.declare_parameter("inf_epsilon", 1.0)
        # Per-lidar planar calibration (same numbers the old merger got via
        # laser_*_offset): fitted once by ICP against a shared wall.
        self.declare_parameter("laser_1_x_offset", 0.0)
        self.declare_parameter("laser_1_y_offset", 0.0)
        self.declare_parameter("laser_1_yaw_offset", 0.0)
        self.declare_parameter("laser_2_x_offset", 0.0)
        self.declare_parameter("laser_2_y_offset", 0.0)
        self.declare_parameter("laser_2_yaw_offset", 0.0)

        gp = self.get_parameter
        self._target_frame = gp("target_frame").value
        self._fixed_frame = gp("fixed_frame").value
        self._pair_tolerance = float(gp("pair_tolerance").value)
        self._scan_time = gp("scan_time").value
        self._tf_timeout = Duration(seconds=gp("tf_timeout").value)
        self._angle_min = gp("angle_min").value
        self._angle_max = gp("angle_max").value
        self._angle_increment = gp("angle_increment").value
        self._range_min = gp("range_min").value
        self._range_max = gp("range_max").value
        self._use_inf = gp("use_inf").value
        self._fill = float("inf") if self._use_inf else self._range_max + gp("inf_epsilon").value
        self._calibration = [
            smm.make_planar_transform(
                *(gp(f"laser_{i}_{axis}_offset").value for axis in ("x", "y", "yaw"))
            )
            for i in (1, 2)
        ]

        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)

        # Each slot: (msg, recv_monotonic) or None.
        self._latest = [None, None]
        self._last_published_pair = None
        self._static_cache = {}

        # Pub: SensorDataQoS (BEST_EFFORT) — what Nav2 costmaps / AMCL /
        # collision_monitor subscribe with. Sub: RELIABLE — blankers publish
        # the rclpy default (RELIABLE); BEST_EFFORT sub would silently get
        # nothing from them.
        pub_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )
        sub_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )
        self._pub = self.create_publisher(LaserScan, gp("output_topic").value, pub_qos)
        topics = [gp("laser_1_topic").value, gp("laser_2_topic").value]
        for idx, topic in enumerate(topics):
            self.create_subscription(
                LaserScan, topic, lambda msg, i=idx: self._on_scan(i, msg), sub_qos
            )

        self.get_logger().info(
            f"merging {topics[0]} + {topics[1]} -> {gp('output_topic').value} "
            f"[{self._target_frame}], deskew via {self._fixed_frame} TF, "
            f"pair_tolerance={self._pair_tolerance:.3f} s (wall-clock)"
        )

    def _static_matrix(self, frame_id, calibration):
        """Return cached 4x4 base<-laser matrix (static TF composed with calibration)."""
        if frame_id not in self._static_cache:
            tf = self._tf_buffer.lookup_transform(
                self._target_frame, frame_id, Time(), timeout=self._tf_timeout
            )
            self._static_cache[frame_id] = _tf_matrix(tf) @ calibration
        return self._static_cache[frame_id]

    def _odom_to_base(self, stamp_ns, latest_ns):
        """Look up odom<-target_frame at stamp_ns, clamped to the latest available TF."""
        clamped = min(stamp_ns, latest_ns)
        tf = self._tf_buffer.lookup_transform(
            self._fixed_frame,
            self._target_frame,
            Time(nanoseconds=int(clamped)),
            timeout=self._tf_timeout,
        )
        return _tf_matrix(tf)

    def _sweep_center_ns(self, msg):
        """Return the instant the scan content is centered on (sweep midpoint)."""
        scan_time = msg.scan_time if msg.scan_time > 0.0 else self._scan_time
        return _stamp_ns(msg.header.stamp) + int(scan_time * 1e9 / 2)

    def _on_scan(self, idx, msg):
        """Merge when the partner is fresh by wall-clock; publish once per pair."""
        now = time.monotonic()
        self._latest[idx] = (msg, now)
        other_slot = self._latest[1 - idx]
        if other_slot is None:
            return  # other lidar not up yet (e.g. intentional 5 s right delay)
        other, other_recv = other_slot
        age = now - other_recv
        if age > self._pair_tolerance:
            self.get_logger().warn(
                f"partner scan not received for {age:.2f} s "
                f"(>{self._pair_tolerance:.2f} s) — a lidar may be down",
                throttle_duration_sec=5.0,
            )
            return

        # Emit once per unique pair, on the later stamp — halves CPU vs merging
        # on every arrival from both sides (~20 Hz → ~10 Hz).
        my_stamp = _stamp_ns(msg.header.stamp)
        other_stamp = _stamp_ns(other.header.stamp)
        if my_stamp < other_stamp:
            return
        pair_key = (
            _stamp_ns(self._latest[0][0].header.stamp),
            _stamp_ns(self._latest[1][0].header.stamp),
        )
        if pair_key == self._last_published_pair:
            return

        scans = (self._latest[0][0], self._latest[1][0])
        try:
            latest_tf = self._tf_buffer.lookup_transform(
                self._fixed_frame, self._target_frame, Time(), timeout=self._tf_timeout
            )
            latest_ns = _stamp_ns(latest_tf.header.stamp)

            t_odom_base = []
            points = []
            centers = []
            for scan, calib in zip(scans, self._calibration, strict=True):
                center = self._sweep_center_ns(scan)
                centers.append(center)
                t_odom_base.append(self._odom_to_base(center, latest_ns))
                static = self._static_matrix(scan.header.frame_id, calib)
                pts = smm.polar_to_points(
                    scan.ranges,
                    scan.angle_min,
                    scan.angle_increment,
                    scan.range_min,
                    scan.range_max,
                )
                points.append(smm.apply_transform(pts, static))

            t_common = min((centers[0] + centers[1]) // 2, latest_ns)
            t_odom_common = self._odom_to_base(t_common, latest_ns)

            merged_points = np.vstack(
                [
                    smm.apply_transform(pts, smm.deskew_delta(t_ob, t_odom_common))
                    for pts, t_ob in zip(points, t_odom_base, strict=True)
                ]
            )
        except TransformException as ex:
            self.get_logger().warn(
                f"TF lookup failed, dropping scan: {ex}", throttle_duration_sec=5.0
            )
            return

        out = LaserScan()
        out.header.stamp = Time(nanoseconds=int(t_common)).to_msg()
        out.header.frame_id = self._target_frame
        out.angle_min = float(self._angle_min)
        out.angle_max = float(self._angle_max)
        out.angle_increment = float(self._angle_increment)
        out.time_increment = 0.0
        out.scan_time = float(self._scan_time)
        out.range_min = float(self._range_min)
        out.range_max = float(self._range_max)
        out.ranges = smm.bin_points(
            merged_points,
            self._angle_min,
            self._angle_max,
            self._angle_increment,
            self._range_min,
            self._range_max,
            self._fill,
        ).tolist()
        self._pub.publish(out)
        self._last_published_pair = pair_key


def main(args=None):
    """Run the scan merger node until interrupted."""
    rclpy.init(args=args)
    node = ScanMerger()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
