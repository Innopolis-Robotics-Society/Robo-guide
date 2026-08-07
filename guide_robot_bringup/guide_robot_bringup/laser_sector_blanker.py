#!/usr/bin/env python3
"""Blank out fixed angular sectors of a LaserScan (e.g. self-hits on the robot frame).

Each RPLIDAR sees its own mounting bar / the other lidar's mount at a fixed
bearing relative to its own frame, on every scan, regardless of the real
environment. That bearing is a property of the physical mount, not something
SLAM/Nav2 should treat as an obstacle, so it must be removed from /scan_left
and /scan_right *before* dual_laser_merger combines them — the merger only
clips the merged output's angle_min/angle_max (message ends), it does not
mask a wedge inside a single input's field of view.

Sector angles are in each lidar's own frame (same convention as the raw
/scan_left, /scan_right topics), found empirically with
laser_blind_sector_finder — see that node's docstring.
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


def _normalize(angle):
    """Wrap an angle into [-pi, pi)."""
    two_pi = 2.0 * 3.141592653589793
    return (angle + 3.141592653589793) % two_pi - 3.141592653589793


def _in_sector(angle, lo, hi):
    """Return True if angle (already normalized) falls within [lo, hi], wrapping through +-pi."""
    if lo <= hi:
        return lo <= angle <= hi
    return angle >= lo or angle <= hi  # sector crosses the -pi/pi seam


class LaserSectorBlanker(Node):
    """Republishes a LaserScan with configured angular sectors set to infinity."""

    def __init__(self):
        """Declare parameters and set up the subscription/publisher pair."""
        super().__init__("laser_sector_blanker")

        self.declare_parameter("input_topic", "scan_in")
        self.declare_parameter("output_topic", "scan_out")
        # Comma-separated "start_deg,end_deg,start_deg,end_deg,..." pairs, own
        # frame - a string, not a double[], because rclpy can't infer a type
        # for an empty-list default and "" (no sectors yet) must be valid.
        self.declare_parameter("blind_sectors_deg", "")

        input_topic = self.get_parameter("input_topic").value
        output_topic = self.get_parameter("output_topic").value
        raw = self.get_parameter("blind_sectors_deg").value.strip()
        sectors_deg = [float(x) for x in raw.split(",") if x.strip()] if raw else []

        if len(sectors_deg) % 2 != 0:
            raise ValueError("blind_sectors_deg must contain an even number of values (pairs)")

        self._sectors_rad = [
            (_normalize(_deg2rad(lo)), _normalize(_deg2rad(hi)))
            for lo, hi in zip(sectors_deg[0::2], sectors_deg[1::2], strict=False)
        ]

        self._sub = self.create_subscription(LaserScan, input_topic, self._callback, 10)
        self._pub = self.create_publisher(LaserScan, output_topic, 10)

        if self._sectors_rad:
            self.get_logger().info(
                f"{input_topic} -> {output_topic}: blanking {len(self._sectors_rad)} sector(s) "
                f"{sectors_deg} deg"
            )
        else:
            self.get_logger().warn(
                f"{input_topic} -> {output_topic}: no blind_sectors_deg configured, passing "
                "scan through unmodified. Calibrate with laser_blind_sector_finder."
            )

    def _callback(self, msg):
        """Blank configured sectors in the incoming scan and republish it."""
        if self._sectors_rad:
            for i in range(len(msg.ranges)):
                angle = _normalize(msg.angle_min + i * msg.angle_increment)
                if any(_in_sector(angle, lo, hi) for lo, hi in self._sectors_rad):
                    msg.ranges[i] = float("inf")
                    if i < len(msg.intensities):
                        msg.intensities[i] = 0.0
        self._pub.publish(msg)


def _deg2rad(deg):
    return deg * 3.141592653589793 / 180.0


def main(args=None):
    """Run the laser sector blanker node until interrupted."""
    rclpy.init(args=args)
    node = LaserSectorBlanker()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
