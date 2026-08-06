"""Bridge a sensor-data LaserScan topic to reliable QoS for Cartographer.

``dual_laser_merger`` publishes its merged scan with SensorDataQoS
(BEST_EFFORT), while Cartographer ROS 2 subscribes with RELIABLE QoS.  A
BEST_EFFORT publisher cannot satisfy a RELIABLE subscription, so Cartographer
would silently receive no scans.  This relay keeps the normal sensor topic for
Nav2 and republishes the same messages on a Cartographer-only reliable topic.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import LaserScan


_SENSOR_QOS = QoSProfile(
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=QoSReliabilityPolicy.BEST_EFFORT,
    durability=QoSDurabilityPolicy.VOLATILE,
)

_RELIABLE_QOS = QoSProfile(
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=QoSReliabilityPolicy.RELIABLE,
    durability=QoSDurabilityPolicy.VOLATILE,
)


class ScanQosRelay(Node):
    """Republish LaserScan messages without modifying payload or timestamps."""

    def __init__(self) -> None:
        super().__init__("scan_qos_relay")

        input_topic = str(self.declare_parameter("input_topic", "/scan").value)
        output_topic = str(
            self.declare_parameter("output_topic", "/scan_cartographer").value
        )

        self._publisher = self.create_publisher(
            LaserScan, output_topic, _RELIABLE_QOS
        )
        self._subscription = self.create_subscription(
            LaserScan, input_topic, self._on_scan, _SENSOR_QOS
        )

        self.get_logger().info(
            f"bridging BEST_EFFORT {input_topic} -> RELIABLE {output_topic}"
        )

    def _on_scan(self, message: LaserScan) -> None:
        self._publisher.publish(message)


def main(args=None) -> None:
    """Run the scan QoS relay."""
    rclpy.init(args=args)
    node = ScanQosRelay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
