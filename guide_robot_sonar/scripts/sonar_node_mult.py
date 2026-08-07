#!/usr/bin/env python3
"""
ROS 2 wrapper node for Guide-Robot sonar sensors using a low-level C++ driver.

Publishes one sensor_msgs/Range topic per sensor:
    sonar/range/<frame_id>

SensorDataQoS (BEST_EFFORT, depth 1) is required, not a preference:
nav2_collision_monitor subscribes to range sources with rclcpp::SensorDataQoS(),
and a RELIABLE publisher will not match a BEST_EFFORT subscriber in ROS 2 —
the topic silently never connects.

Filtering (see _RangeFilter) is latency-bounded on purpose since these feed
a safety loop: median(3) rejects single-frame crosstalk spikes, a deadband
kills the residual hop without the settling time of a moving average, and
hysteresis around max_range stops the in/out-of-range decision flip-flopping
for a target sitting right at the edge.

End-to-end latency is dominated by hardware: 7 sensors round-robin on one
UART with a settle delay between pings, so a given sensor's cached reading
refreshes only every ~200-250 ms regardless of update_rate. Publishing faster
just republishes the latest cached value — expected for a steady-rate topic.

Bus-failure fail-safe: a raw read error (-1 from the driver) means the bus
request/response itself failed, which is a different fact than "echo not
returned" (0xFFFF, i.e. nothing in range). Collapsing both into the same
"no detection" value would make nav2_collision_monitor read a dead/disconnected
sonar as "path clear". Instead, once a sensor accumulates
`bus_error_threshold` consecutive failed polls, this node stops trusting it and
force-publishes min_range (an obstacle right in front of it) until a
non-error read arrives, and reports it as an ERROR in /diagnostics.

The error count comes from the driver (SonarReading.consecutive_errors) and is
per bus transaction, not per publish tick: one sensor is polled roughly every
~250 ms while this node publishes at update_rate (20 Hz), so counting errors
here would inflate a single failed transaction into ~5 and trip the fail-safe
on one lost checksum. SonarReading.seq is used the same way to keep the
diagnostic status counters per-poll rather than per-tick.

Staleness fail-safe: every message is stamped with the publish time, not the
acquisition time, so a reading that stopped refreshing would keep looking
fresh to nav2 and its source_timeout would never fire. SonarReading.age_s
closes that: past `max_reading_age_s` the sensor takes the same min_range
fail-safe as a bus fault. This is what catches the failures the per-sensor
error count cannot see — driver thread wedged, port never opened at all.
"""

import statistics
from collections import deque

import furo_sonars_cpp
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import Range
from sonar_mapping import SONAR_MAPPING

# Matches rclcpp::SensorDataQoS() used by nav2_collision_monitor.
# Do not change to RELIABLE.
_SENSOR_QOS = QoSProfile(
    reliability=QoSReliabilityPolicy.BEST_EFFORT,
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=1,
    durability=QoSDurabilityPolicy.VOLATILE,
)


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
            # Clamp to max_range: the hysteresis above deliberately lets the
            # in-range decision lag behind median_m near the boundary, but the
            # published value itself must stay within [.., max_range] to keep
            # the sensor_msgs/Range contract — a naive consumer doing
            # `range > max_range` to detect "out of range" must not see a
            # valid-looking in-range reading that is actually past max_range.
            self._last_published = min(median_m, max_range)

        return self._last_published


class SonarNode(Node):
    """Polls sonar sensors via serial and publishes one Range topic per sensor."""

    def __init__(self):
        """Declare parameters and set up subscriptions, publisher, and timer."""
        super().__init__("sonar_node_mult")

        self.declare_parameter("port", "/dev/tty_sonar")
        self.declare_parameter("baudrate", 9600)
        self.declare_parameter("update_rate", 20.0)
        self.declare_parameter("min_range", 0.1)
        self.declare_parameter("max_range", 2.0)
        self.declare_parameter("fov", 1.13)  # 65 deg (from robot XML config)
        self.declare_parameter("topic_prefix", "sonar/range")
        self.declare_parameter("filter_window_size", 3)
        self.declare_parameter("deadband_m", 0.03)
        self.declare_parameter("range_hysteresis_m", 0.05)
        self.declare_parameter("hold_time_s", 0.6)
        # collision_monitor treats a reading outside [min_range, max_range] as
        # "no detection". +inf is the sensor_msgs/Range convention, but
        # max_range + eps is more portable across consumers that do a naive
        # numeric comparison and choke on inf.
        self.declare_parameter("publish_inf_as_out_of_range", True)
        # Драйвер отдаёт время пролёта эха, а не миллиметры
        self.declare_parameter("range_scale", 0.0026104)
        self.declare_parameter("range_offset", -0.01303)
        self.declare_parameter("status_log_period_s", 0.0)
        # Consecutive failed polls of one sensor (driver-side count) before it
        # is considered failed rather than just "nothing in range right now".
        # Unit is bus transactions, ~250 ms apart, so 3 ≈ 0.75 s of silence
        # from that sensor before the fail-safe engages. Tune against real bus
        # behavior — this default has not been validated against sustained
        # hardware faults.
        self.declare_parameter("bus_error_threshold", 3)
        # Age of the driver's cached reading past which the sensor is treated
        # as failed. A healthy round is ~250 ms and ~400 ms worst case (7
        # sensors x 30 ms response budget + 30 ms settle each), so this leaves
        # roughly two rounds of margin. Lower it only against a measured round
        # time on the real dome.
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
        self._topic_prefix = self.get_parameter("topic_prefix").get_parameter_value().string_value
        self._hold_time_s = self.get_parameter("hold_time_s").get_parameter_value().double_value
        self._min_range = self.get_parameter("min_range").get_parameter_value().double_value
        self._max_range = self.get_parameter("max_range").get_parameter_value().double_value
        self._fov = self.get_parameter("fov").get_parameter_value().double_value
        self._publish_inf = (
            self.get_parameter("publish_inf_as_out_of_range").get_parameter_value().bool_value
        )
        self._range_scale = self.get_parameter("range_scale").get_parameter_value().double_value
        self._range_offset = self.get_parameter("range_offset").get_parameter_value().double_value
        self._bus_error_threshold = (
            self.get_parameter("bus_error_threshold").get_parameter_value().integer_value
        )
        self._max_reading_age_s = (
            self.get_parameter("max_reading_age_s").get_parameter_value().double_value
        )
        self._no_detection_value = float("inf") if self._publish_inf else self._max_range + 0.01

        self.sonar_mapping = SONAR_MAPPING

        self.filters = {
            s_id: _RangeFilter(
                self._filter_window, self._deadband_m, self._range_hysteresis_m, self._hold_time_s
            )
            for s_id in self.sonar_mapping
        }
        # Latest driver-side consecutive-error count per sensor, mirrored here
        # only so the diagnostics timer can report it.
        self._consecutive_bus_errors = {s_id: 0 for s_id in self.sonar_mapping}
        self._bus_failed = {s_id: False for s_id in self.sonar_mapping}
        # Last driver poll counter seen per sensor, so each bus transaction is
        # counted once instead of once per publish tick.
        self._last_seq = {s_id: 0 for s_id in self.sonar_mapping}
        # Age of the last reading and whether it exceeded max_reading_age_s,
        # kept for the diagnostics timer.
        self._reading_age_s = {s_id: 0.0 for s_id in self.sonar_mapping}
        self._stale = {s_id: False for s_id in self.sonar_mapping}
        # Last fault state logged per sensor, so the console gets one line per
        # transition instead of one per publish tick.
        self._last_fault_state = {s_id: "" for s_id in self.sonar_mapping}

        prefix = self._topic_prefix.rstrip("/")
        self.publishers_by_id = {}
        for s_id, frame_id in self.sonar_mapping.items():
            topic = f"{prefix}/{frame_id}"
            self.publishers_by_id[s_id] = self.create_publisher(Range, topic, _SENSOR_QOS)
            self.get_logger().info(f"Sonar {s_id} -> {topic} (frame: {frame_id})")

        self.diagnostics_pub = self.create_publisher(DiagnosticArray, "diagnostics", 10)

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

        # [эхо, нет эха (0xFFFF), ошибка чтения (-1)] на каждый датчик
        self._status = {s_id: [0, 0, 0] for s_id in self.sonar_mapping}
        status_period = self.get_parameter("status_log_period_s").value
        if status_period > 0.0:
            self.create_timer(status_period, self._log_status)

        self.timer = self.create_timer(1.0 / self._update_rate, self.publish_ranges)
        diagnostics_period = self.get_parameter("diagnostics_period_s").value
        self.create_timer(diagnostics_period, self._publish_diagnostics)
        self.get_logger().info(f"Sonar node initialized: {len(self.sonar_mapping)} sensors.")

    def _require_driver_api(self):
        """Refuse to run against a furo_sonars_cpp build older than the node.

        Degrading gracefully here would be the wrong call: without
        get_readings() there is no per-poll error count and no reading age, so
        both fail-safes would quietly never fire and an unplugged dome would
        keep publishing "path clear". The .so and this script ship in the same
        package and are always installed together, so a mismatch can only mean
        a partial rebuild — fail loudly at start-up instead.
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
        """Log fault transitions once, on the edge.

        The fail-safe is otherwise only visible on /diagnostics, which nothing
        in this stack subscribes to — pulling the sonar cable produced no
        console output at all.
        """
        for s_id, frame_id in self.sonar_mapping.items():
            # Compare the fault *kind* only. Including the error count here
            # made every extra failed poll look like a new state and printed
            # a line per publish tick.
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

    def _log_status(self):
        """Доля тиков в каждом состоянии за период, по датчикам."""
        parts = []
        for s_id, frame in self.sonar_mapping.items():
            ok, no_echo, err = self._status[s_id]
            total = max(1, ok + no_echo + err)
            parts.append(
                f"{frame.rsplit('_', 1)[-1]}: эхо {100 * ok // total}% "
                f"нет {100 * no_echo // total}% ошибок {100 * err // total}%"
            )
            self._status[s_id] = [0, 0, 0]
        self.get_logger().info(" | ".join(parts))

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

    def publish_ranges(self):
        """Fetch latest ranges from the C++ driver, filter, and publish."""
        readings = self.driver.get_readings()
        now_time = self.get_clock().now()
        now = now_time.to_msg()
        now_s = now_time.nanoseconds * 1e-9

        for s_id, frame_id in self.sonar_mapping.items():
            reading = readings.get(s_id)
            # seq == 0 means the driver has not polled this sensor yet (range
            # is -1 but no transaction has failed) — it must not read as a bus
            # fault during the first poll round after start-up. A driver that
            # never polls at all is caught by age_s instead, which the driver
            # counts from start().
            counts = reading.range if reading is not None else -1
            self._consecutive_bus_errors[s_id] = (
                reading.consecutive_errors if reading is not None else 0
            )
            seq = reading.seq if reading is not None else 0
            self._reading_age_s[s_id] = reading.age_s if reading is not None else float("inf")

            # Count each poll once: the cached value is republished several
            # times between two polls of the same sensor.
            if seq != self._last_seq[s_id]:
                self._last_seq[s_id] = seq
                if counts == 0xFFFF:
                    self._status[s_id][1] += 1
                elif counts < 0:
                    self._status[s_id][2] += 1
                else:
                    self._status[s_id][0] += 1

            if counts < 0 or counts == 0xFFFF:
                raw_m = float("inf")
            else:
                # max(): offset отрицательный, на малых counts даёт минус.
                raw_m = max(0.0, counts * self._range_scale + self._range_offset)

            self._bus_failed[s_id] = (
                self._consecutive_bus_errors[s_id] >= self._bus_error_threshold
            )
            self._stale[s_id] = self._reading_age_s[s_id] > self._max_reading_age_s

            filtered_m = self.filters[s_id].update(raw_m, self._max_range, now_s)

            if self._bus_failed[s_id] or self._stale[s_id]:
                # Fail-safe: a dead/disconnected sensor, or one whose reading
                # stopped refreshing, must not read as "path clear" to
                # nav2_collision_monitor. Report the closest measurable
                # distance instead, so the stop/slowdown polygons treat it as
                # an obstacle until the sensor recovers.
                range_m = self._min_range
            else:
                # The filter's internal "no detection" sentinel is always
                # inf; translate it once, here, to the configured wire value.
                range_m = filtered_m if filtered_m != float("inf") else self._no_detection_value
                # Enforce the sensor_msgs/Range contract: a valid numeric
                # range must not read below min_range either.
                if range_m != float("inf"):
                    range_m = max(range_m, self._min_range)

            msg = Range()
            msg.header.stamp = now
            msg.header.frame_id = frame_id
            msg.radiation_type = Range.ULTRASOUND
            msg.field_of_view = self._fov
            msg.min_range = self._min_range
            msg.max_range = self._max_range
            msg.range = range_m

            self.publishers_by_id[s_id].publish(msg)

        self._report_fault_changes()

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
