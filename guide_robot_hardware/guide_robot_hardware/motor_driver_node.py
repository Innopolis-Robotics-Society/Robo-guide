import math

import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from tf2_ros import TransformBroadcaster


class MotorDriverNode(Node):
    """Differential-drive motor driver: converts cmd_vel to wheel speeds and publishes odometry."""

    def __init__(self):
        """Initialize subscriptions, publishers and robot/state parameters."""
        super().__init__("motor_driver_node")
        self.create_subscription(Twist, "cmd_vel", self.cmd_vel_callback, 10)
        self.odom_pub = self.create_publisher(Odometry, "odom", 10)
        self.wheel_base = 0.338
        self.wheel_radius = 0.075
        self.left_encoder = (
            0  # (м/с) #(will be implemented later, just need to read serial port from driver
        )
        self.right_encoder = 0  # (м/с)
        self.tf_broadcaster = TransformBroadcaster(self)  #
        # position and speeds of a robot
        self.dt = 0.05
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0
        self.v_linear_x = 0.0
        self.w_angular_z = 0.0

        self.timer = self.create_timer(self.dt, self.timer_callback)
        # integrating speeds to obtain a position

    def timer_callback(self):
        """Integrate speeds into a pose estimate and publish odometry and TF."""
        self.actual_speeds()
        self.theta = self.theta + self.w_angular_z * self.dt
        self.dx = self.v_linear_x * math.cos(self.theta) * self.dt
        self.dy = self.v_linear_x * math.sin(self.theta) * self.dt
        self.x = self.x + self.dx
        self.y = self.y + self.dy
        self.odometry_publish()
        self.publish_tf()

    def actual_speeds(self):
        """Convert wheel speeds from encoders to the body's linear and angular velocities."""
        v_left_actual = self.left_encoder * self.wheel_radius
        v_right_actual = self.right_encoder * self.wheel_radius

        self.v_linear_x = (v_right_actual + v_left_actual) / 2.0
        self.w_angular_z = (v_right_actual - v_left_actual) / self.wheel_base

    def cmd_vel_callback(self, msg: Twist):
        """Convert a cmd_vel Twist into per-wheel angular speeds."""
        linear = msg.linear.x
        angular = msg.angular.z
        v_left = linear - (angular * self.wheel_base / 2)
        v_right = linear + (angular * self.wheel_base / 2)
        w_left = v_left / self.wheel_radius
        w_right = v_right / self.wheel_radius

        # there will be implemented  sending signals to motor driver using serial port/can
        self.get_logger().info(f"w_left:{w_left} w_right:{w_right}")

    def odometry_publish(self):
        """Publish the current pose and velocity estimate on the odom topic."""
        odom = Odometry()

        # time and frames
        odom.header.stamp = self.get_clock().now().to_msg()
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_footprint"

        # position
        odom.pose.pose.position.x = self.x
        odom.pose.pose.position.y = self.y
        odom.pose.pose.position.z = 0.0

        # Convert self.theta to quaternion
        odom.pose.pose.orientation.x = 0.0
        odom.pose.pose.orientation.y = 0.0
        odom.pose.pose.orientation.z = math.sin(self.theta / 2.0)
        odom.pose.pose.orientation.w = math.cos(self.theta / 2.0)

        # velocities
        odom.twist.twist.linear.x = self.v_linear_x
        odom.twist.twist.angular.z = self.w_angular_z

        # Publish message to topic /odom
        self.odom_pub.publish(odom)

    def publish_tf(self):
        """Broadcast the odom -> base_footprint transform."""
        t = TransformStamped()
        t.header.stamp = self.get_clock().now().to_msg()
        t.header.frame_id = "odom"
        t.child_frame_id = "base_footprint"

        t.transform.translation.x = self.x
        t.transform.translation.y = self.y
        t.transform.translation.z = 0.0

        t.transform.rotation.x = 0.0
        t.transform.rotation.y = 0.0
        t.transform.rotation.z = math.sin(self.theta / 2.0)
        t.transform.rotation.w = math.cos(self.theta / 2.0)

        self.tf_broadcaster.sendTransform(t)


def main(args=None):
    """Initialize rclpy, spin the MotorDriverNode, and shut down cleanly."""
    rclpy.init(args=args)
    node = MotorDriverNode()
    rclpy.spin(node)

    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
