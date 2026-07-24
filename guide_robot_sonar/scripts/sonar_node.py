#!/usr/bin/env python3
"""
ROS 2 wrapper node for FURO-D sonar sensors using a low-level C++ driver.

Filtering strategy (see _RangeFilter) — kept deliberately latency-bounded
since these sensors are intended to feed a safety loop later on:
    1. Median filter (window=3) over raw readings. Rejects the occasional
       single-frame outlier (acoustic crosstalk between adjacent sensors)
       without averaging/EMA-style stages that accumulate multi-tick
       convergence delay.
    2. Deadband: the published value only moves when the new median
       differs from it by more than `deadband_m`. This is what actually
       kills the ~10-15 cm hop a median alone leaves when raw readings
       flip-flop between two close values as the window slides — but
       unlike a moving average, a genuine change is reported immediately
       (no settling time), so it doesn't add latency on top of the median.
    3. Range-boundary hysteresis: a target sitting right at `max_range`
       (typical for sensors aimed down open corridors) can cause the
       in/out-of-range decision to flip every cycle, which reads as the
       cone marker rapidly appearing/disappearing. `range_hysteresis_m`
       requires the median to clear max_range by a margin before the
       in-range state flips, in either direction.

    Worst-case end-to-end latency is dominated by the hardware: 7 sensors
    round-robin on one UART with a settle delay between pings (see
    sonar_driver.cpp), so a single sensor's cached reading refreshes only
    every ~200-250 ms regardless of this node's update_rate. Publishing
    faster than that just republishes the latest cached value, which is
    the expected/normal behavior for a steady-rate safety topic.
"""

import math
import statistics
from collections import deque

import furo_sonars_cpp
import rclpy
from builtin_interfaces.msg import Duration
from geometry_msgs.msg import Point
from rclpy.node import Node
from sensor_msgs.msg import Range
from visualization_msgs.msg import Marker, MarkerArray

from guide_robot_msgs.msg import SonarRanges

# Number of triangular facets around the cone circumference for 3D markers
_CONE_SEGMENTS = 16


class _RangeFilter:
    """Per-sensor filter: median (reject spikes) -> deadband + range hysteresis."""

    def __init__(self, window_size, deadband_m, range_hysteresis_m):
        self._history = deque(maxlen=window_size)
        self._deadband_m = deadband_m
        self._range_hysteresis_m = range_hysteresis_m
        self._last_published = float("inf")
        self._in_range = False

    def update(self, raw_m, max_range):
        """Feed one raw reading (meters, or inf) and return the filtered range."""
        self._history.append(raw_m)
        finite = [v for v in self._history if v != float("inf")]
        median_m = statistics.median(finite) if finite else float("inf")

        # Schmitt-trigger around max_range so a target sitting right at the
        # edge doesn't flip the in/out-of-range decision every cycle.
        if self._in_range:
            if median_m > max_range + self._range_hysteresis_m:
                self._in_range = False
        else:
            if median_m <= max_range - self._range_hysteresis_m:
                self._in_range = True

        if not self._in_range:
            self._last_published = float("inf")
            return float("inf")

        if self._last_published == float("inf") or (
            abs(median_m - self._last_published) >= self._deadband_m
        ):
            self._last_published = median_m

        return self._last_published


class SonarNode(Node):
    """ROS 2 node that polls sonar sensors via serial and publishes ranges."""

    def __init__(self):
        """Initialize the SonarNode and start the C++ driver."""
        super().__init__("sonar_node")

        # ── Parameters ──────────────────────────────────────────────────
        self.declare_parameter("port", "/dev/tty_sonar")
        self.declare_parameter("baudrate", 9600)
        self.declare_parameter("update_rate", 20.0)
        self.declare_parameter("min_range", 0.1)
        self.declare_parameter("max_range", 2.0)
        self.declare_parameter("fov", 1.13)  # 65 deg (from robot XML config)
        self.declare_parameter("publish_individual_topics", False)
        self.declare_parameter("filter_window_size", 3)
        self.declare_parameter("deadband_m", 0.03)
        self.declare_parameter("range_hysteresis_m", 0.05)

        port = self.get_parameter("port").get_parameter_value().string_value
        baudrate = self.get_parameter("baudrate").get_parameter_value().integer_value
        update_rate = self.get_parameter("update_rate").get_parameter_value().double_value
        filter_window = (
            self.get_parameter("filter_window_size").get_parameter_value().integer_value
        )
        deadband_m = self.get_parameter("deadband_m").get_parameter_value().double_value
        range_hysteresis_m = (
            self.get_parameter("range_hysteresis_m").get_parameter_value().double_value
        )

        # ── Sonar ID → URDF frame mapping ───────────────────────────────
        self.sonar_mapping = {
            0: "sonar_sensor_1",  # Front Left
            1: "sonar_sensor_2",  # Left Front
            2: "sonar_sensor_4",  # Left Rear
            3: "sonar_sensor_5",  # Rear Center
            4: "sonar_sensor_6",  # Right Rear
            5: "sonar_sensor_8",  # Right Front
            6: "sonar_sensor_9",  # Front Right
        }

        # ── Per-sensor filters ───────────────────────────────────────────
        self.filters = {
            s_id: _RangeFilter(filter_window, deadband_m, range_hysteresis_m)
            for s_id in self.sonar_mapping
        }

        # ── Publishers ──────────────────────────────────────────────────
        self.publisher = self.create_publisher(SonarRanges, "sonar/ranges", 10)
        self.marker_publisher = self.create_publisher(MarkerArray, "sonar/markers", 10)

        self.individual_publishers = {}
        for s_id, frame_id in self.sonar_mapping.items():
            self.individual_publishers[s_id] = self.create_publisher(
                Range, f"sonar/{frame_id}", 10
            )

        # ── C++ serial driver ───────────────────────────────────────────
        self.get_logger().info(f"Starting C++ driver on {port} @ {baudrate} baud...")
        self.driver = furo_sonars_cpp.SonarDriver(port, baudrate)
        self.driver.start()

        # ── Timer ───────────────────────────────────────────────────────
        self.timer = self.create_timer(1.0 / update_rate, self.publish_ranges)
        self.get_logger().info("Sonar node initialized.")

    # ─────────────────────────────────────────────────────────────────────
    #  Main publish callback
    # ─────────────────────────────────────────────────────────────────────
    def publish_ranges(self):
        """Fetch latest ranges from the C++ driver, filter, and publish."""
        ranges_data = self.driver.get_ranges()
        now = self.get_clock().now().to_msg()

        min_range = self.get_parameter("min_range").get_parameter_value().double_value
        max_range = self.get_parameter("max_range").get_parameter_value().double_value
        fov = self.get_parameter("fov").get_parameter_value().double_value
        update_rate = self.get_parameter("update_rate").get_parameter_value().double_value
        publish_individual = (
            self.get_parameter("publish_individual_topics").get_parameter_value().bool_value
        )

        # Marker lifetime is tied to the actual publish period with margin,
        # not a hardcoded constant: if it were pinned near the nominal period,
        # ordinary timer jitter makes a marker expire in RViz/Foxglove right
        # before its replacement arrives, which reads as every cone flickering
        # in sync every time a publish is a few ms late.
        marker_lifetime_s = max(0.3, 3.0 / update_rate) if update_rate > 0 else 0.3

        sonar_ranges_msg = SonarRanges()
        sonar_ranges_msg.header.stamp = now
        sonar_ranges_msg.header.frame_id = "base_link"

        marker_array = MarkerArray()

        for s_id, frame_id in self.sonar_mapping.items():
            # ── Raw measurement ─────────────────────────────────────────
            val_mm = ranges_data.get(s_id, -1)
            if val_mm == 0xFFFF or val_mm < 0:
                raw_m = float("inf")
            else:
                raw_m = val_mm / 1000.0

            # ── Median + deadband/hysteresis filter ─────────────────────
            range_m = self.filters[s_id].update(raw_m, max_range)

            # ── Range message ───────────────────────────────────────────
            range_msg = Range()
            range_msg.header.stamp = now
            range_msg.header.frame_id = frame_id
            range_msg.radiation_type = Range.ULTRASOUND
            range_msg.field_of_view = fov
            range_msg.min_range = min_range
            range_msg.max_range = max_range
            range_msg.range = range_m

            sonar_ranges_msg.ranges.append(range_msg)

            if publish_individual:
                self.individual_publishers[s_id].publish(range_msg)

            # ── 3D cone marker ──────────────────────────────────────────
            marker = self._build_cone_marker(
                now, frame_id, s_id, range_m, max_range, fov, marker_lifetime_s
            )
            marker_array.markers.append(marker)

        self.publisher.publish(sonar_ranges_msg)
        self.marker_publisher.publish(marker_array)

    # ─────────────────────────────────────────────────────────────────────
    #  Cone marker builder
    # ─────────────────────────────────────────────────────────────────────
    def _build_cone_marker(self, stamp, frame_id, marker_id, range_m, max_range, fov, lifetime_s):
        """
        Build a TRIANGLE_LIST cone marker for one sonar sensor.

        Returns a Marker that is either ADD (visible cone) or DELETE
        (no obstacle / out of range).
        """
        marker = Marker()
        marker.header.stamp = stamp
        marker.header.frame_id = frame_id
        marker.ns = "sonar"
        marker.id = marker_id
        marker.type = Marker.TRIANGLE_LIST

        if range_m == float("inf") or range_m > max_range:
            marker.action = Marker.DELETE
            return marker

        marker.action = Marker.ADD

        # Pose: identity (cone originates at the sensor frame origin)
        marker.pose.orientation.w = 1.0

        # Scale must be 1.0 for TRIANGLE_LIST (vertices are in meters)
        marker.scale.x = 1.0
        marker.scale.y = 1.0
        marker.scale.z = 1.0

        # Semi-transparent orange
        marker.color.r = 1.0
        marker.color.g = 0.6
        marker.color.b = 0.0
        marker.color.a = 0.35

        # ── Geometry: cone apex at origin, base at X = range_m ──────
        tip = Point(x=0.0, y=0.0, z=0.0)
        radius = max(0.01, range_m) * math.tan(fov / 2.0)
        base_center = Point(x=range_m, y=0.0, z=0.0)

        # Pre-compute base circle vertices
        base_pts = []
        for i in range(_CONE_SEGMENTS):
            angle = 2.0 * math.pi * i / _CONE_SEGMENTS
            base_pts.append(
                Point(
                    x=range_m,
                    y=radius * math.cos(angle),
                    z=radius * math.sin(angle),
                )
            )

        # Build triangles (side + base cap per segment)
        for i in range(_CONE_SEGMENTS):
            p1 = base_pts[i]
            p2 = base_pts[(i + 1) % _CONE_SEGMENTS]

            # Side face: tip → p1 → p2
            marker.points.append(tip)
            marker.points.append(p1)
            marker.points.append(p2)

            # Base cap face: p1 → base_center → p2
            marker.points.append(p1)
            marker.points.append(base_center)
            marker.points.append(p2)

        lifetime_sec = int(lifetime_s)
        marker.lifetime = Duration(
            sec=lifetime_sec, nanosec=int((lifetime_s - lifetime_sec) * 1e9)
        )
        return marker

    # ─────────────────────────────────────────────────────────────────────
    #  Cleanup
    # ─────────────────────────────────────────────────────────────────────
    def destroy_node(self):
        """Stop the C++ serial driver thread and destroy the node."""
        self.get_logger().info("Stopping low-level driver...")
        self.driver.stop()
        super().destroy_node()


def main(args=None):
    """Execute the sonar node."""
    rclpy.init(args=args)
    node = SonarNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
