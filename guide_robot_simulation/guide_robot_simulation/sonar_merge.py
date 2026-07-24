#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range

from guide_robot_msgs.msg import SonarRanges


class SonarAggregator(Node):
    """Collects individual sensor_msgs/Range topics into one SonarRanges msg.

    Publishes at a fixed rate using the latest value from each sensor.
    Stale readings (older than timeout) are dropped so a dead sensor does not
    freeze an obstacle into the message forever.
    """

    def __init__(self):
        """Declare parameters and set up subscriptions, publisher, and timer."""
        super().__init__("sonar_aggregator")

        self.declare_parameter(
            "sensor_ids",
            ["sonar_1", "sonar_2", "sonar_4", "sonar_5", "sonar_6", "sonar_8", "sonar_9"],
        )
        self.declare_parameter("input_prefix", "sonar/raw")
        self.declare_parameter("output_topic", "sonars")
        self.declare_parameter("frame_id", "base_link")
        self.declare_parameter("publish_rate", 10.0)
        self.declare_parameter("timeout", 0.5)

        ids = self.get_parameter("sensor_ids").value
        prefix = self.get_parameter("input_prefix").value
        self.frame_id = self.get_parameter("frame_id").value
        rate = self.get_parameter("publish_rate").value
        self.timeout = self.get_parameter("timeout").value

        self.ids = list(ids)
        self.latest = {}  # id -> Range
        self.subs = []
        for sid in self.ids:
            self.subs.append(
                self.create_subscription(
                    Range, f"{prefix}/{sid}", lambda msg, s=sid: self.cb(msg, s), 10
                )
            )

        self.pub = self.create_publisher(SonarRanges, self.get_parameter("output_topic").value, 10)
        self.timer = self.create_timer(1.0 / rate, self.publish)

        self.get_logger().info(f"Aggregating {len(self.ids)} sonars: {self.ids}")

    def cb(self, msg, sid):
        """Cache the latest Range reading for the given sensor id."""
        self.latest[sid] = msg

    def publish(self):
        """Publish a SonarRanges message built from non-stale cached readings."""
        now = self.get_clock().now()
        out = SonarRanges()
        out.header.stamp = now.to_msg()
        out.header.frame_id = self.frame_id

        for sid in self.ids:  # stable order
            r = self.latest.get(sid)
            if r is None:
                continue
            age = (now - rclpy.time.Time.from_msg(r.header.stamp)).nanoseconds * 1e-9
            if age > self.timeout:
                continue
            out.ranges.append(r)

        self.pub.publish(out)


def main():
    """Initialize the node, spin, and shut down cleanly."""
    rclpy.init()
    node = SonarAggregator()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
