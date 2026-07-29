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
"""

import statistics
from collections import deque

import furo_sonars_cpp
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import Range

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
            self._last_published = median_m

        return self._last_published


class SonarNode(Node):
    """Polls sonar sensors via serial and publishes one Range topic per sensor."""

    def __init__(self):
        """Declare parameters and set up subscriptions, publisher, and timer."""
        super().__init__("sonar_node")

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
        topic_prefix = self.get_parameter("topic_prefix").get_parameter_value().string_value

        # Sonar ID → URDF frame. Frame numbering is non-contiguous because it
        # follows the original Guide-Robot harness labels, not the driver poll order.
        self.sonar_mapping = {
            0: "sonar_sensor_1",  # Front Left
            1: "sonar_sensor_2",  # Left Front
            2: "sonar_sensor_4",  # Left Rear
            3: "sonar_sensor_5",  # Rear Center
            4: "sonar_sensor_6",  # Right Rear
            5: "sonar_sensor_8",  # Right Front
            6: "sonar_sensor_9",  # Front Right
        }

        hold_time_s = self.get_parameter("hold_time_s").get_parameter_value().double_value
        self.filters = {
            s_id: _RangeFilter(filter_window, deadband_m, range_hysteresis_m, hold_time_s)
            for s_id in self.sonar_mapping
        }

        prefix = topic_prefix.rstrip("/")
        self.publishers_by_id = {}
        for s_id, frame_id in self.sonar_mapping.items():
            topic = f"{prefix}/{frame_id}"
            self.publishers_by_id[s_id] = self.create_publisher(Range, topic, _SENSOR_QOS)
            self.get_logger().info(f"Sonar {s_id} -> {topic} (frame: {frame_id})")

        self.get_logger().info(f"Starting C++ driver on {port} @ {baudrate} baud...")
        self.driver = furo_sonars_cpp.SonarDriver(port, baudrate)
        self.driver.start()

        # [эхо, нет эха (0xFFFF), ошибка чтения (-1)] на каждый датчик
        self._status = {s_id: [0, 0, 0] for s_id in self.sonar_mapping}
        status_period = self.get_parameter("status_log_period_s").value
        if status_period > 0.0:
            self.create_timer(status_period, self._log_status)

        self.timer = self.create_timer(1.0 / update_rate, self.publish_ranges)
        self.get_logger().info(f"Sonar node initialized: {len(self.sonar_mapping)} sensors.")

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

    def publish_ranges(self):
        """Fetch latest ranges from the C++ driver, filter, and publish."""
        ranges_data = self.driver.get_ranges()
        now_time = self.get_clock().now()
        now = now_time.to_msg()
        now_s = now_time.nanoseconds * 1e-9

        min_range = self.get_parameter("min_range").get_parameter_value().double_value
        max_range = self.get_parameter("max_range").get_parameter_value().double_value
        fov = self.get_parameter("fov").get_parameter_value().double_value
        publish_inf = (
            self.get_parameter("publish_inf_as_out_of_range").get_parameter_value().bool_value
        )
        range_scale = self.get_parameter("range_scale").get_parameter_value().double_value
        range_offset = self.get_parameter("range_offset").get_parameter_value().double_value

        no_detection_value = float("inf") if publish_inf else max_range + 0.01

        for s_id, frame_id in self.sonar_mapping.items():
            counts = ranges_data.get(s_id, -1)
            if counts == 0xFFFF:
                self._status[s_id][1] += 1
                raw_m = float("inf")
            elif counts < 0:
                self._status[s_id][2] += 1
                raw_m = float("inf")
            else:
                self._status[s_id][0] += 1
                # max(): offset отрицательный, на малых counts даёт минус.
                raw_m = max(0.0, counts * range_scale + range_offset)

            filtered_m = self.filters[s_id].update(raw_m, max_range, now_s)

            # The filter's internal "no detection" sentinel is always inf;
            # translate it once, here, to the configured wire value.
            range_m = filtered_m if filtered_m != float("inf") else no_detection_value

            msg = Range()
            msg.header.stamp = now
            msg.header.frame_id = frame_id
            msg.radiation_type = Range.ULTRASOUND
            msg.field_of_view = fov
            msg.min_range = min_range
            msg.max_range = max_range
            msg.range = range_m

            self.publishers_by_id[s_id].publish(msg)

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
