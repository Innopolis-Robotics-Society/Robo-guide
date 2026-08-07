#!/usr/bin/env python3
r"""Empirically find the angular sector(s) where a lidar sees its own mount/frame.

A single static capture CANNOT tell a self-hit on the robot's own structure
apart from a real object that just happens to be sitting nearby and not
moving - both look identical to "close range, present on every scan, barely
jitters". The discriminator that actually works is motion of the ROBOT, not
of the environment: a self-hit is rigidly attached to the lidar, so it stays
at the exact same bearing *and* the exact same range no matter how the robot
is moved or turned, while a real object's range/bearing relative to the
sensor changes as the robot moves relative to it (the lidar sits off the
robot's rotation axis by the mount offset, so even pure in-place rotation
already moves it relative to any external point).

So: capture with the robot actively being rotated/pushed around during the
whole window, not held still - it is fine (better, even) if there is normal
clutter in the room. This node bins ranges by angle over that window and
flags bins that are near-constant, close-range, and present on almost every
scan as "blind sector" candidates; with the robot moving, only bins that are
truly fixed to the frame can pass that bar.

Usage (run against one lidar at a time; during the whole capture window,
physically rotate the robot a full turn and shift its position a bit,
slowly, so any nearby object's bearing/range visibly changes):

    ros2 run guide_robot_bringup laser_blind_sector_finder --ros-args \\
        -p topic:=/scan_left -p duration_sec:=45.0

Then repeat for /scan_right (rotate/move again during that capture too - the
first capture's motion doesn't carry over). The printed blind_sectors_deg
line is already a ready-to-paste value ("start_deg,end_deg,...") in that
lidar's own frame convention, matching what laser_sector_blanker expects for
the corresponding input.
"""

import math
import statistics

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan

_RAD2DEG = 180.0 / 3.141592653589793


class LaserBlindSectorFinder(Node):
    """Collects scans for a fixed window, then reports candidate blind sectors."""

    def __init__(self):
        """Declare parameters and subscribe to the target scan topic."""
        super().__init__("laser_blind_sector_finder")

        self.declare_parameter("topic", "scan_left")
        self.declare_parameter("duration_sec", 45.0)
        self.declare_parameter("max_range_for_suspect", 1.0)
        self.declare_parameter("stdev_threshold", 0.02)
        self.declare_parameter("coverage_threshold", 0.9)

        topic = self.get_parameter("topic").value
        self._duration = self.get_parameter("duration_sec").value
        self._max_range = self.get_parameter("max_range_for_suspect").value
        self._stdev_thresh = self.get_parameter("stdev_threshold").value
        self._coverage_thresh = self.get_parameter("coverage_threshold").value

        self._angle_min = None
        self._angle_increment = None
        self._samples = None  # list[list[float]], per angle bin
        self._scan_count = 0

        self._sub = self.create_subscription(LaserScan, topic, self._callback, 10)
        self._timer = self.create_timer(self._duration, self._finish)

        self.get_logger().info(
            f"Listening on {topic} for {self._duration:.0f}s. Rotate/move the ROBOT ITSELF "
            "continuously during this window - a fixed bearing+range survives that, a real "
            "object's bearing/range relative to the sensor won't."
        )

    def _callback(self, msg):
        """Accumulate one scan's ranges into their per-bin history."""
        if self._samples is None:
            self._angle_min = msg.angle_min
            self._angle_increment = msg.angle_increment
            self._samples = [[] for _ in msg.ranges]

        if len(msg.ranges) != len(self._samples):
            self.get_logger().warn("Scan size changed mid-capture, dropping this scan.")
            return

        self._scan_count += 1
        for i, r in enumerate(msg.ranges):
            if math.isfinite(r):
                self._samples[i].append(r)

        if self._scan_count % 20 == 0:
            self.get_logger().info(f"...{self._scan_count} scans collected")

    def _finish(self):
        """Analyze accumulated data, print candidate sectors, and shut down."""
        self._timer.cancel()
        if not self._samples or self._scan_count == 0:
            self.get_logger().error(
                "No scans received - check the topic name and that the " "lidar is publishing."
            )
            rclpy.shutdown()
            return

        suspect = []
        for readings in self._samples:
            coverage = len(readings) / self._scan_count
            if len(readings) < 2 or coverage < self._coverage_thresh:
                suspect.append(False)
                continue
            mean = statistics.mean(readings)
            stdev = statistics.pstdev(readings)
            suspect.append(mean <= self._max_range and stdev <= self._stdev_thresh)

        sectors = self._group(suspect)

        self.get_logger().info(
            f"Analyzed {self._scan_count} scans, {len(self._samples)} angle bins. "
            f"Found {len(sectors)} candidate blind sector(s)."
        )
        flat_deg = []
        for start, end in sectors:
            span = list(self._span(start, end))
            readings = [r for i in span for r in self._samples[i]]
            mean = statistics.mean(readings)
            stdev = statistics.pstdev(readings)
            lo_deg = self._angle_deg(start)
            hi_deg = self._angle_deg(end)
            self.get_logger().info(
                f"  [{lo_deg:7.2f} deg, {hi_deg:7.2f} deg]  "
                f"range {mean:.3f}m +-{stdev:.3f}m  ({len(span)} bins)"
            )
            flat_deg.extend([lo_deg, hi_deg])

        if flat_deg:
            sectors_str = ",".join(f"{v:.2f}" for v in flat_deg)
            self.get_logger().info(f'blind_sectors_deg: "{sectors_str}"')
        else:
            self.get_logger().info(
                "No stable close-range sector found - try lowering coverage_threshold/"
                "stdev_threshold, or check the lidar really does see its own mount."
            )

        rclpy.shutdown()

    def _angle_deg(self, index):
        return (self._angle_min + index * self._angle_increment) * _RAD2DEG

    def _span(self, start, end):
        n = len(self._samples)
        if start <= end:
            return range(start, end + 1)
        return list(range(start, n)) + list(range(0, end + 1))  # wraps across -pi/pi seam

    @staticmethod
    def _group(suspect):
        """Group a boolean-per-bin array into contiguous (start, end) index ranges, wrap-aware."""
        n = len(suspect)
        if not any(suspect):
            return []

        sectors = []
        i = 0
        visited = [False] * n
        while i < n:
            if suspect[i] and not visited[i]:
                start = i
                j = i
                while suspect[j % n] and not visited[j % n] and j - i < n:
                    visited[j % n] = True
                    j += 1
                sectors.append((start, (j - 1) % n))
                i = j
            else:
                i += 1

        # A sector ending at the last bin and one starting at bin 0 are one seam-crossing run.
        if len(sectors) > 1 and sectors[0][0] == 0 and sectors[-1][1] == n - 1:
            first = sectors.pop(0)
            last = sectors.pop(-1)
            sectors.append((last[0], first[1]))

        return sectors


def main(args=None):
    """Run the blind sector finder until its capture window elapses."""
    rclpy.init(args=args)
    node = LaserBlindSectorFinder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            node.destroy_node()
            rclpy.shutdown()


if __name__ == "__main__":
    main()
