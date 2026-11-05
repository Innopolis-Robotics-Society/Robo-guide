"""ROS map snapshots and validated pose command conversion."""

import math

from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

from guide_robot_api.lib.map_geometry import OccupancyMap, quaternion_yaw
from guide_robot_api.lib.qos import QOS_MISSION_STATE


class MapAdapter:
    """Capture ROS map/TF independently from any UI assets."""

    def __init__(self, node):
        """Initialize the adapter and its configured resources."""
        self.node = node
        self.current = None
        self.target = None
        node.create_subscription(
            PoseStamped, "/mission/target_pose", self.receive_target, QOS_MISSION_STATE
        )
        self.buffer = Buffer(node=node)
        self.listener = TransformListener(self.buffer, node)
        node.create_subscription(OccupancyGrid, "/map", self.receive, QOS_MISSION_STATE)

    def receive_target(self, msg):
        """Observe the current mission target."""
        self.target = (
            None
            if not msg.header.frame_id
            else {
                "x": msg.pose.position.x,
                "y": msg.pose.position.y,
                "yaw": quaternion_yaw(msg.pose.orientation),
            }
        )

    def receive(self, msg):
        """Capture the latest valid map."""
        try:
            self.current = OccupancyMap(msg)
        except ValueError as exc:
            self.node.get_logger().warning(str(exc))

    def state(self):
        """Read public map or network state without credentials."""
        snapshot = self.current
        if snapshot is None:
            return {"available": False, "robot_pose": None}
        result = snapshot.metadata()
        result["target"] = self.target
        result["robot_pose"] = None
        try:
            transform = self.buffer.lookup_transform(snapshot.frame, "base_link", Time())
            age = (
                self.node.get_clock().now() - Time.from_msg(transform.header.stamp)
            ).nanoseconds / 1e9
            p, q = transform.transform.translation, transform.transform.rotation
            result["robot_pose"] = {
                "x": p.x,
                "y": p.y,
                "yaw": quaternion_yaw(q),
                "age_s": age,
                "fresh": 0 <= age <= 3,
            }
        except TransformException:
            pass
        return result

    def pose(self, body, *, free):
        """Convert validated planar coordinates to a ROS pose."""
        snapshot = self.current
        if snapshot is None:
            raise ValueError("map_unavailable")
        snapshot.validate_pose(body["pose"], body["map_version"], free=free)
        pose = PoseStamped()
        pose.header.frame_id = snapshot.frame
        pose.header.stamp = self.node.get_clock().now().to_msg()
        pose.pose.position.x = float(body["pose"]["x"])
        pose.pose.position.y = float(body["pose"]["y"])
        pose.pose.orientation.z = math.sin(body["pose"]["yaw"] / 2)
        pose.pose.orientation.w = math.cos(body["pose"]["yaw"] / 2)
        return pose
