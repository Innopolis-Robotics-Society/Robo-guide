"""
Publish explicit Cartographer submap extents for RViz.

Cartographer's RViz plugin alpha-blends the texture of each submap.  Unknown
cells are transparent, so the texture's rectangular extent is not visible.
This node queries the same texture metadata and publishes a colored outline
and a size label for every submap as a ``MarkerArray``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from functools import partial

import rclpy
from cartographer_ros_msgs.msg import SubmapEntry, SubmapList
from cartographer_ros_msgs.srv import SubmapQuery
from geometry_msgs.msg import Point, Pose
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from visualization_msgs.msg import Marker, MarkerArray


_SUBMAP_LIST_QOS = QoSProfile(
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=5,
    reliability=QoSReliabilityPolicy.RELIABLE,
    durability=QoSDurabilityPolicy.VOLATILE,
)

_MARKER_QOS = QoSProfile(
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=QoSReliabilityPolicy.RELIABLE,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
)

# Distinct neighboring submaps are easier to follow than one shared color.
_COLORS = (
    (0.00, 0.85, 1.00),
    (1.00, 0.48, 0.05),
    (0.35, 0.95, 0.20),
    (0.95, 0.20, 0.80),
    (0.20, 0.45, 1.00),
    (1.00, 0.85, 0.05),
    (1.00, 0.20, 0.20),
    (0.55, 0.30, 1.00),
)

SubmapId = tuple[int, int]


@dataclass(frozen=True)
class TextureBounds:
    """Geometry from the high-resolution texture returned by SubmapQuery."""

    version: int
    width: int
    height: int
    resolution: float
    slice_pose: Pose


def _rotate_vector(pose: Pose, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Rotate a point by a pose quaternion without an extra tf dependency."""
    q = pose.orientation
    norm = math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w)
    if norm < 1e-12:
        return point

    qx = q.x / norm
    qy = q.y / norm
    qz = q.z / norm
    qw = q.w / norm
    x, y, z = point

    # Quaternion-vector rotation: v' = v + w * t + q_xyz x t,
    # where t = 2 * (q_xyz x v).
    tx = 2.0 * (qy * z - qz * y)
    ty = 2.0 * (qz * x - qx * z)
    tz = 2.0 * (qx * y - qy * x)
    return (
        x + qw * tx + qy * tz - qz * ty,
        y + qw * ty + qz * tx - qx * tz,
        z + qw * tz + qx * ty - qy * tx,
    )


def _transform_point(pose: Pose, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply a geometry_msgs Pose transform to a 3D point."""
    x, y, z = _rotate_vector(pose, point)
    return x + pose.position.x, y + pose.position.y, z + pose.position.z


class SubmapBoundaryVisualizer(Node):
    """Query submap texture bounds and publish them as persistent markers."""

    def __init__(self) -> None:
        super().__init__("submap_boundary_visualizer")

        submap_list_topic = str(
            self.declare_parameter("submap_list_topic", "/submap_list").value
        )
        query_service = str(
            self.declare_parameter("submap_query_service", "/submap_query").value
        )
        marker_topic = str(
            self.declare_parameter("marker_topic", "/submap_boundaries").value
        )
        self._line_width = float(self.declare_parameter("line_width", 0.04).value)
        self._z_offset = float(self.declare_parameter("z_offset", 0.05).value)
        self._show_labels = bool(self.declare_parameter("show_labels", True).value)
        self._label_height = float(self.declare_parameter("label_height", 0.20).value)
        self._max_in_flight = max(
            1, int(self.declare_parameter("max_queries_in_flight", 2).value)
        )

        self._publisher = self.create_publisher(MarkerArray, marker_topic, _MARKER_QOS)
        self._subscription = self.create_subscription(
            SubmapList, submap_list_topic, self._on_submap_list, _SUBMAP_LIST_QOS
        )
        self._query_client = self.create_client(SubmapQuery, query_service)

        self._frame_id = "map"
        self._entries: dict[SubmapId, SubmapEntry] = {}
        self._bounds: dict[SubmapId, TextureBounds] = {}
        self._pending: dict[SubmapId, int] = {}
        self._retry_after: dict[SubmapId, float] = {}
        self._marker_ids: dict[SubmapId, int] = {}
        self._next_marker_id = 0
        self._published_keys: set[SubmapId] = set()
        self._service_wait_logged = False

        # A timer fills the small request pool and retries if Cartographer was
        # not ready when this node started.
        self._query_timer = self.create_timer(0.2, self._schedule_queries)

        self.get_logger().info(
            f"visualizing all Cartographer submap bounds: {submap_list_topic} + "
            f"{query_service} -> {marker_topic}"
        )

    def _on_submap_list(self, message: SubmapList) -> None:
        if message.header.frame_id:
            self._frame_id = message.header.frame_id

        new_entries: dict[SubmapId, SubmapEntry] = {
            (entry.trajectory_id, entry.submap_index): entry for entry in message.submap
        }
        # Versions normally only increase.  A decrease means Cartographer was
        # restarted and IDs are being reused, so old texture metadata is no
        # longer valid.
        if any(
            key in self._entries
            and self._entries[key].submap_version > entry.submap_version
            for key, entry in new_entries.items()
        ):
            self._bounds.clear()
            self._retry_after.clear()

        removed = self._entries.keys() - new_entries.keys()
        for key in removed:
            self._bounds.pop(key, None)
            self._retry_after.pop(key, None)

        self._entries = new_entries
        self._publish_markers()
        self._schedule_queries()

    def _schedule_queries(self) -> None:
        if len(self._pending) >= self._max_in_flight or not self._entries:
            return

        if not self._query_client.service_is_ready():
            if not self._service_wait_logged:
                self.get_logger().info("waiting for Cartographer service /submap_query")
                self._service_wait_logged = True
            return
        self._service_wait_logged = False

        now = time.monotonic()
        # Newest submaps first: their bounds are the most useful while mapping.
        for key in sorted(self._entries, reverse=True):
            if len(self._pending) >= self._max_in_flight:
                break

            entry = self._entries[key]
            cached = self._bounds.get(key)
            # The service can return a version newer than the SubmapList that
            # triggered the request.  It is already up to date in that case.
            if cached is not None and cached.version >= entry.submap_version:
                continue
            if key in self._pending or self._retry_after.get(key, 0.0) > now:
                continue

            request = SubmapQuery.Request()
            request.trajectory_id, request.submap_index = key
            requested_version = entry.submap_version
            self._pending[key] = requested_version
            future = self._query_client.call_async(request)
            future.add_done_callback(
                partial(
                    self._on_query_complete,
                    key=key,
                    requested_version=requested_version,
                )
            )

    def _on_query_complete(self, future, *, key: SubmapId, requested_version: int) -> None:
        self._pending.pop(key, None)
        try:
            response = future.result()
        except Exception as error:  # rclpy futures surface transport errors here.
            self._retry_after[key] = time.monotonic() + 2.0
            self.get_logger().warning(f"submap query failed for {key}: {error}")
            return

        if key not in self._entries:
            return
        if response is None or not response.textures:
            self._retry_after[key] = time.monotonic() + 2.0
            self.get_logger().warning(f"submap query returned no texture for {key}")
            return

        # Texture 0 is the high-resolution slice used by SubmapsDisplay.
        texture = response.textures[0]
        response_version = int(response.submap_version)
        self._bounds[key] = TextureBounds(
            version=response_version if response_version >= 0 else requested_version,
            width=int(texture.width),
            height=int(texture.height),
            resolution=float(texture.resolution),
            slice_pose=texture.slice_pose,
        )
        self._retry_after.pop(key, None)
        self._publish_markers()
        self._schedule_queries()

    def _marker_id_pair(self, key: SubmapId) -> tuple[int, int]:
        first = self._marker_ids.get(key)
        if first is None:
            first = self._next_marker_id
            self._marker_ids[key] = first
            self._next_marker_id += 2
        return first, first + 1

    def _submap_corners(self, entry: SubmapEntry, bounds: TextureBounds) -> list[Point]:
        metric_width = bounds.resolution * bounds.width
        metric_height = bounds.resolution * bounds.height

        # These are the exact quad corners used by cartographer_rviz/OgreSlice.
        local_corners = (
            (0.0, 0.0, 0.0),
            (-metric_height, 0.0, 0.0),
            (-metric_height, -metric_width, 0.0),
            (0.0, -metric_width, 0.0),
            (0.0, 0.0, 0.0),
        )

        points = []
        for corner in local_corners:
            in_submap = _transform_point(bounds.slice_pose, corner)
            x, y, z = _transform_point(entry.pose, in_submap)
            points.append(Point(x=x, y=y, z=z + self._z_offset))
        return points

    def _make_outline(self, key: SubmapId, entry: SubmapEntry, bounds: TextureBounds) -> Marker:
        outline_id, _ = self._marker_id_pair(key)
        marker = Marker()
        marker.header.frame_id = self._frame_id
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.ns = "submap_boundaries"
        marker.id = outline_id
        marker.type = Marker.LINE_STRIP
        marker.action = Marker.ADD
        marker.pose.orientation.w = 1.0
        marker.scale.x = self._line_width
        marker.points = self._submap_corners(entry, bounds)

        red, green, blue = _COLORS[(key[0] * 3 + key[1]) % len(_COLORS)]
        marker.color.r = red
        marker.color.g = green
        marker.color.b = blue
        marker.color.a = 0.95
        return marker

    def _make_label(self, key: SubmapId, outline: Marker, bounds: TextureBounds) -> Marker:
        _, label_id = self._marker_id_pair(key)
        marker = Marker()
        marker.header = outline.header
        marker.ns = "submap_labels"
        marker.id = label_id
        marker.type = Marker.TEXT_VIEW_FACING
        marker.action = Marker.ADD
        marker.pose.orientation.w = 1.0

        corners = outline.points[:4]
        marker.pose.position.x = sum(point.x for point in corners) / 4.0
        marker.pose.position.y = sum(point.y for point in corners) / 4.0
        marker.pose.position.z = sum(point.z for point in corners) / 4.0 + 0.12
        marker.scale.z = self._label_height
        marker.color.r = outline.color.r
        marker.color.g = outline.color.g
        marker.color.b = outline.color.b
        marker.color.a = 1.0

        metric_width = bounds.resolution * bounds.width
        metric_height = bounds.resolution * bounds.height
        marker.text = (
            f"T{key[0]}/S{key[1]}  "
            f"{metric_width:.1f} x {metric_height:.1f} m"
        )
        return marker

    def _make_delete(self, key: SubmapId, namespace: str, marker_id: int) -> Marker:
        marker = Marker()
        marker.header.frame_id = self._frame_id
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.ns = namespace
        marker.id = marker_id
        marker.action = Marker.DELETE
        return marker

    def _publish_markers(self) -> None:
        markers = MarkerArray()
        drawable_keys = self._entries.keys() & self._bounds.keys()

        for key in sorted(drawable_keys):
            entry = self._entries[key]
            bounds = self._bounds[key]
            outline = self._make_outline(key, entry, bounds)
            markers.markers.append(outline)
            if self._show_labels:
                markers.markers.append(self._make_label(key, outline, bounds))

        for key in sorted(self._published_keys - drawable_keys):
            outline_id, label_id = self._marker_id_pair(key)
            markers.markers.append(
                self._make_delete(key, "submap_boundaries", outline_id)
            )
            markers.markers.append(self._make_delete(key, "submap_labels", label_id))

        self._published_keys = set(drawable_keys)
        if markers.markers:
            self._publisher.publish(markers)


def main(args=None) -> None:
    """Run the submap boundary visualizer."""
    rclpy.init(args=args)
    node = SubmapBoundaryVisualizer()
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
