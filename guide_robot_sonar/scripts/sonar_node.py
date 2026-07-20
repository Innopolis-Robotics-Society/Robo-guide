#!/usr/bin/env python3
"""ROS 2 wrapper node for FURO-D sonar sensors using a low-level C++ driver."""

# Import compiled pybind11 module
import furo_sonars_cpp
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range

from guide_robot_msgs.msg import SonarRanges


class SonarNode(Node):
    """ROS 2 node that polls sonar sensors via serial and publishes ranges."""

    def __init__(self):
        """Initialize the SonarNode and start the C++ driver."""
        super().__init__("sonar_node")

        # Declare parameters
        self.declare_parameter("port", "/dev/ttyCH341USB0")
        self.declare_parameter("baudrate", 9600)
        self.declare_parameter("update_rate", 20.0)
        self.declare_parameter("min_range", 0.1)
        self.declare_parameter("max_range", 2.0)
        self.declare_parameter("fov", 0.26)  # ~15 deg

        # Get parameters
        port = self.get_parameter("port").get_parameter_value().string_value
        baudrate = self.get_parameter("baudrate").get_parameter_value().integer_value
        update_rate = self.get_parameter("update_rate").get_parameter_value().double_value

        # Mapping from driver sonar IDs [0..6] to URDF frames
        self.sonar_mapping = {
            0: "sonar_sensor_1",  # Front Left
            1: "sonar_sensor_2",  # Left Front
            2: "sonar_sensor_4",  # Left Rear
            3: "sonar_sensor_5",  # Rear Center
            4: "sonar_sensor_6",  # Right Rear
            5: "sonar_sensor_8",  # Right Front
            6: "sonar_sensor_9",  # Front Right
        }

        # Initialize publisher
        self.publisher = self.create_publisher(SonarRanges, "sonar/ranges", 10)

        # Initialize and start low-level C++ driver
        self.get_logger().info(f"Starting low-level C++ driver on {port} at {baudrate}...")
        self.driver = furo_sonars_cpp.SonarDriver(port, baudrate)
        self.driver.start()

        # Start timer for publishing
        period = 1.0 / update_rate
        self.timer = self.create_timer(period, self.publish_ranges)
        self.get_logger().info("Sonar node successfully initialized.")

    def publish_ranges(self):
        """Fetch latest ranges from driver and publish them."""
        ranges_data = self.driver.get_ranges()
        now = self.get_clock().now().to_msg()

        min_range = self.get_parameter("min_range").get_parameter_value().double_value
        max_range = self.get_parameter("max_range").get_parameter_value().double_value
        fov = self.get_parameter("fov").get_parameter_value().double_value

        msg = SonarRanges()
        msg.header.stamp = now
        msg.header.frame_id = "base_link"

        for s_id, frame_id in self.sonar_mapping.items():
            val_mm = ranges_data.get(s_id, -1)

            # Determine range value in meters
            if val_mm == 0xFFFF or val_mm < 0:
                range_m = float("inf")
            else:
                range_m = val_mm / 1000.0

            # Create individual Range message
            range_msg = Range()
            range_msg.header.stamp = now
            range_msg.header.frame_id = frame_id
            range_msg.radiation_type = Range.ULTRASOUND
            range_msg.field_of_view = fov
            range_msg.min_range = min_range
            range_msg.max_range = max_range
            range_msg.range = range_m

            msg.ranges.append(range_msg)

        self.publisher.publish(msg)

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
