#!/usr/bin/env python3
"""
ROS 2 wrapper node for Guide-Robot sonar sensors using a low-level C++ driver.

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

Bus-failure fail-safe: see sonar_node_mult.py for the rationale — a raw
read error (-1) is treated as a sensor fault, not "nothing detected", and
forces min_range + an ERROR in /diagnostics once it persists for
`bus_error_threshold` consecutive polls. The count comes from the driver
(SonarReading.consecutive_errors) so it tracks bus transactions rather than
this node's publish ticks, which are ~5x more frequent. A reading older than
`max_reading_age_s` (SonarReading.age_s) takes the same fail-safe: header
stamps are publish time, so a reading that stopped refreshing would otherwise
stay indistinguishable from a current one.
"""

import math
import statistics
from collections import deque

import furo_sonars_cpp
import rclpy
from builtin_interfaces.msg import Duration
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from geometry_msgs.msg import Point
from rclpy.node import Node
from sensor_msgs.msg import Range
from sonar_mapping import SONAR_MAPPING
from visualization_msgs.msg import Marker, MarkerArray

from guide_robot_msgs.msg import SonarRanges

# Number of triangular facets around the cone circumference for 3D markers
_CONE_SEGMENTS = 16


class _RangeFilter:
    """Медиана по различающимся опросам -> удержание детекта -> deadband."""

    def __init__(self, window_size, deadband_m, range_hysteresis_m, hold_time_s):
        self._history = deque(maxlen=window_size)
        self._deadband_m = deadband_m
        self._range_hysteresis_m = range_hysteresis_m
        self._hold_time_s = hold_time_s
        self._last_published = float("inf")
        self._last_finite_t = None
        self._in_range = False

    def update(self, raw_m, max_range, now_s):
        """Feed one raw reading (meters, or inf) and return the filtered range."""
        if raw_m != float("inf"):
            self._last_finite_t = now_s
            if not self._history or raw_m != self._history[-1]:
                self._history.append(raw_m)
        elif self._last_finite_t is None or now_s - self._last_finite_t >= self._hold_time_s:
            self._history.clear()
            self._in_range = False
            self._last_published = float("inf")
            return float("inf")

        if not self._history:
            return float("inf")

        median_m = statistics.median_low(self._history)

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
            # Clamp to max_range — see sonar_node_mult.py for why: the
            # hysteresis above is allowed to lag near the boundary, but the
            # published value must stay within the sensor_msgs/Range contract.
            self._last_published = min(median_m, max_range)

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
        # Сколько держать последний детект после пропажи эха. 0 — выключено.
        self.declare_parameter("hold_time_s", 0.6)
        # Драйвер отдаёт время пролёта эха, а не миллиметры
        self.declare_parameter("range_scale", 0.0026104)
        self.declare_parameter("range_offset", -0.01303)
        # Consecutive failed polls of one sensor (driver-side count) before it
        # is considered failed rather than just "nothing in range right now".
        # Unit is bus transactions, ~250 ms apart.
        self.declare_parameter("bus_error_threshold", 3)
        # Age of the driver's cached reading past which the sensor is treated
        # as failed — see sonar_node_mult.py for how the limit was picked.
        self.declare_parameter("max_reading_age_s", 0.75)
        self.declare_parameter("diagnostics_period_s", 1.0)

        self._port = self.get_parameter("port").get_parameter_value().string_value
        self._baudrate = self.get_parameter("baudrate").get_parameter_value().integer_value
        self._update_rate = self.get_parameter("update_rate").get_parameter_value().double_value
        self._filter_window = (
            self.get_parameter("filter_window_size").get_parameter_value().integer_value
        )
        self._deadband_m = self.get_parameter("deadband_m").get_parameter_value().double_value
        self._range_hysteresis_m = (
            self.get_parameter("range_hysteresis_m").get_parameter_value().double_value
        )
        self._min_range = self.get_parameter("min_range").get_parameter_value().double_value
        self._max_range = self.get_parameter("max_range").get_parameter_value().double_value
        self._fov = self.get_parameter("fov").get_parameter_value().double_value
        self._publish_individual = (
            self.get_parameter("publish_individual_topics").get_parameter_value().bool_value
        )
        self._range_scale = self.get_parameter("range_scale").get_parameter_value().double_value
        self._range_offset = self.get_parameter("range_offset").get_parameter_value().double_value
        self._hold_time_s = self.get_parameter("hold_time_s").get_parameter_value().double_value
        self._bus_error_threshold = (
            self.get_parameter("bus_error_threshold").get_parameter_value().integer_value
        )
        self._max_reading_age_s = (
            self.get_parameter("max_reading_age_s").get_parameter_value().double_value
        )

        # ── Sonar ID → URDF frame mapping ───────────────────────────────
        self.sonar_mapping = SONAR_MAPPING

        # ── Per-sensor filters ───────────────────────────────────────────
        self.filters = {
            s_id: _RangeFilter(
                self._filter_window, self._deadband_m, self._range_hysteresis_m, self._hold_time_s
            )
            for s_id in self.sonar_mapping
        }
        self._consecutive_bus_errors = {s_id: 0 for s_id in self.sonar_mapping}
        self._bus_failed = {s_id: False for s_id in self.sonar_mapping}
        self._reading_age_s = {s_id: 0.0 for s_id in self.sonar_mapping}
        self._stale = {s_id: False for s_id in self.sonar_mapping}
        self._last_fault_state = {s_id: "" for s_id in self.sonar_mapping}

        # ── Publishers ──────────────────────────────────────────────────
        self.publisher = self.create_publisher(SonarRanges, "sonar/ranges", 10)
        self.marker_publisher = self.create_publisher(MarkerArray, "sonar/markers", 10)
        self.diagnostics_pub = self.create_publisher(DiagnosticArray, "diagnostics", 10)

        self.individual_publishers = {}
        for s_id, frame_id in self.sonar_mapping.items():
            self.individual_publishers[s_id] = self.create_publisher(
                Range, f"sonar/{frame_id}", 10
            )

        # ── C++ serial driver ───────────────────────────────────────────
        self.get_logger().info(f"Starting C++ driver on {self._port} @ {self._baudrate} baud...")
        self.driver = furo_sonars_cpp.SonarDriver(self._port, self._baudrate)
        self._require_driver_api()
        self.driver.set_info_logger(lambda msg: self.get_logger().info(msg))
        self.driver.set_error_logger(lambda msg: self.get_logger().error(msg))
        self.driver.start()

        if not self.driver.is_dome_active():
            self.get_logger().error(
                "Sonar dome activation could not be confirmed sent to the port "
                f"({self._port}) — sensors may not respond."
            )

        # ── Timers ──────────────────────────────────────────────────────
        self.timer = self.create_timer(1.0 / self._update_rate, self.publish_ranges)
        diagnostics_period = self.get_parameter("diagnostics_period_s").value
        self.create_timer(diagnostics_period, self._publish_diagnostics)
        self.get_logger().info("Sonar node initialized.")

    # ─────────────────────────────────────────────────────────────────────
    #  Diagnostics
    # ─────────────────────────────────────────────────────────────────────
    def _require_driver_api(self):
        """Refuse to run against an outdated furo_sonars_cpp build.

        See sonar_node_mult.py: without get_readings() both fail-safes would
        silently never fire, which is worse than not starting at all.
        """
        missing = [
            name
            for name in ("get_readings", "is_dome_active", "set_info_logger", "set_error_logger")
            if not hasattr(self.driver, name)
        ]
        if missing:
            msg = (
                "furo_sonars_cpp is out of date, missing "
                f"{', '.join(missing)}. Loaded from: "
                f"{getattr(furo_sonars_cpp, '__file__', '<unknown>')} — if that path is not "
                "under this package's lib/guide_robot_sonar, a stale copy from an earlier "
                "install is shadowing the built one; delete it (or "
                "rm -rf build/guide_robot_sonar install/guide_robot_sonar) and rebuild. "
                "The sonar fail-safes cannot work against this build."
            )
            self.get_logger().fatal(msg)
            raise RuntimeError(msg)

    def _report_fault_changes(self):
        """Log fault transitions once, on the edge — see sonar_node_mult.py."""
        for s_id, frame_id in self.sonar_mapping.items():
            # Fault kind only — see sonar_node_mult.py: including the count
            # turns every failed poll into a "new" state and spams the log.
            if self._bus_failed[s_id]:
                state = "bus"
            elif self._stale[s_id]:
                state = "stale"
            else:
                state = ""

            if state == self._last_fault_state[s_id]:
                continue
            self._last_fault_state[s_id] = state

            if state == "bus":
                self.get_logger().error(
                    f"{frame_id}: bus failure ({self._consecutive_bus_errors[s_id]} consecutive "
                    f"failed polls) — publishing min_range ({self._min_range:.2f} m) as fail-safe"
                )
            elif state == "stale":
                self.get_logger().error(
                    f"{frame_id}: reading {self._reading_age_s[s_id]:.2f}s old — publishing "
                    f"min_range ({self._min_range:.2f} m) as fail-safe"
                )
            else:
                self.get_logger().info(f"{frame_id}: recovered, publishing measured range again")

    def _publish_diagnostics(self):
        """Publish per-sensor bus-fault status and dome activation health."""
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()

        dome_status = DiagnosticStatus()
        dome_status.name = f"{self.get_name()}: sonar dome"
        dome_status.hardware_id = self._port
        if self.driver.is_dome_active():
            dome_status.level = DiagnosticStatus.OK
            dome_status.message = "wake-up command sent"
        else:
            dome_status.level = DiagnosticStatus.ERROR
            dome_status.message = "wake-up command not confirmed sent to port"
        array.status.append(dome_status)

        for s_id, frame_id in self.sonar_mapping.items():
            status = DiagnosticStatus()
            status.name = f"{self.get_name()}: {frame_id}"
            status.hardware_id = frame_id
            if self._bus_failed[s_id]:
                status.level = DiagnosticStatus.ERROR
                status.message = (
                    f"bus failure: {self._consecutive_bus_errors[s_id]} consecutive failed "
                    "polls, forcing min_range fail-safe"
                )
            elif self._stale[s_id]:
                status.level = DiagnosticStatus.ERROR
                status.message = (
                    f"stale reading: last poll {self._reading_age_s[s_id]:.2f}s ago "
                    f"(limit {self._max_reading_age_s:.2f}s), forcing min_range fail-safe"
                )
            else:
                status.level = DiagnosticStatus.OK
                status.message = "ok"
            array.status.append(status)

        self.diagnostics_pub.publish(array)

    # ─────────────────────────────────────────────────────────────────────
    #  Main publish callback
    # ─────────────────────────────────────────────────────────────────────
    def publish_ranges(self):
        """Fetch latest ranges from the C++ driver, filter, and publish."""
        readings = self.driver.get_readings()
        now_time = self.get_clock().now()
        now = now_time.to_msg()
        now_s = now_time.nanoseconds * 1e-9

        # Marker lifetime is tied to the actual publish period with margin,
        # not a hardcoded constant: if it were pinned near the nominal period,
        # ordinary timer jitter makes a marker expire in RViz/Foxglove right
        # before its replacement arrives, which reads as every cone flickering
        # in sync every time a publish is a few ms late.
        marker_lifetime_s = max(0.3, 3.0 / self._update_rate) if self._update_rate > 0 else 0.3

        sonar_ranges_msg = SonarRanges()
        sonar_ranges_msg.header.stamp = now
        sonar_ranges_msg.header.frame_id = "base_link"

        marker_array = MarkerArray()

        for s_id, frame_id in self.sonar_mapping.items():
            # ── Raw measurement ─────────────────────────────────────────
            reading = readings.get(s_id)
            counts = reading.range if reading is not None else -1
            # Driver-side count: per bus transaction, not per publish tick.
            # Stays 0 until the sensor's first poll completes, so start-up
            # doesn't look like a fault.
            self._consecutive_bus_errors[s_id] = (
                reading.consecutive_errors if reading is not None else 0
            )
            self._reading_age_s[s_id] = reading.age_s if reading is not None else float("inf")

            if counts < 0 or counts == 0xFFFF:
                raw_m = float("inf")
            else:
                # max(): offset отрицательный, на малых counts даёт минус.
                raw_m = max(0.0, counts * self._range_scale + self._range_offset)

            self._bus_failed[s_id] = (
                self._consecutive_bus_errors[s_id] >= self._bus_error_threshold
            )
            self._stale[s_id] = self._reading_age_s[s_id] > self._max_reading_age_s

            # ── Median + deadband/hysteresis filter ─────────────────────
            filtered_m = self.filters[s_id].update(raw_m, self._max_range, now_s)

            if self._bus_failed[s_id] or self._stale[s_id]:
                # Fail-safe: don't let a dead/disconnected sensor read as
                # "path clear" — see sonar_node_mult.py for the rationale.
                range_m = self._min_range
            else:
                range_m = filtered_m
                if range_m != float("inf"):
                    range_m = max(range_m, self._min_range)

            # ── Range message ───────────────────────────────────────────
            range_msg = Range()
            range_msg.header.stamp = now
            range_msg.header.frame_id = frame_id
            range_msg.radiation_type = Range.ULTRASOUND
            range_msg.field_of_view = self._fov
            range_msg.min_range = self._min_range
            range_msg.max_range = self._max_range
            range_msg.range = range_m

            sonar_ranges_msg.ranges.append(range_msg)

            if self._publish_individual:
                self.individual_publishers[s_id].publish(range_msg)

            # ── 3D cone marker ──────────────────────────────────────────
            marker = self._build_cone_marker(
                now, frame_id, s_id, range_m, self._max_range, self._fov, marker_lifetime_s
            )
            marker_array.markers.append(marker)

        self.publisher.publish(sonar_ranges_msg)
        self.marker_publisher.publish(marker_array)
        self._report_fault_changes()

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
