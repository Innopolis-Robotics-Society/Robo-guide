"""Versioned occupancy maps shared by transport and pose validation."""

import hashlib
import math
import struct
import zlib
from typing import Any


def quaternion_yaw(q: Any) -> Any:
    """Extract planar heading from a ROS quaternion."""
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class OccupancyMap:
    """Immutable map snapshot with a stable content version and PNG rendering."""

    def __init__(self, msg: Any) -> None:
        """Capture metadata and cells together to avoid mixed map revisions."""
        info = msg.info
        self.width, self.height = info.width, info.height
        self.resolution = info.resolution
        if not self.width or not self.height or self.resolution <= 0:
            raise ValueError("invalid_map")
        if len(msg.data) != self.width * self.height:
            raise ValueError("invalid_map_size")
        self.cells = tuple(msg.data)
        self.frame = msg.header.frame_id
        self.x, self.y = info.origin.position.x, info.origin.position.y
        self.yaw = quaternion_yaw(info.origin.orientation)
        metadata = (self.width, self.height, self.resolution, self.frame, self.x, self.y, self.yaw)
        self.version = hashlib.sha256(
            repr(metadata).encode() + bytes(v + 1 for v in self.cells)
        ).hexdigest()

    def metadata(self) -> Any:
        """Return public metadata; image is fetched separately."""
        return {
            "available": True,
            "version": self.version,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "frame": self.frame,
            "origin": {"x": self.x, "y": self.y, "yaw": self.yaw},
        }

    def cell(self, x: Any, y: Any) -> Any:
        """Convert world coordinates accounting for a rotated map origin."""
        dx, dy = x - self.x, y - self.y
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return math.floor((c * dx + s * dy) / self.resolution), math.floor(
            (-s * dx + c * dy) / self.resolution
        )

    def validate_pose(self, pose: Any, version: Any, *, free: Any = False) -> Any:
        """Reject stale versions, nonfinite poses and unsuitable target cells."""
        if version != self.version:
            raise ValueError("map_changed")
        if any(
            isinstance(pose[k], bool)
            or not isinstance(pose[k], (float, int))
            or not math.isfinite(pose[k])
            for k in ("x", "y", "yaw")
        ):
            raise ValueError("invalid_pose")
        col, row = self.cell(pose["x"], pose["y"])
        if not (0 <= col < self.width and 0 <= row < self.height):
            raise ValueError("outside_map")
        if free and self.cells[row * self.width + col] != 0:
            raise ValueError("target_not_free")

    def png(self) -> Any:
        """Encode a grayscale PNG without adding an image-library dependency."""

        def chunk(kind: Any, data: Any) -> Any:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        rows = bytearray()
        for row in range(self.height - 1, -1, -1):
            rows.append(0)
            for value in self.cells[row * self.width : (row + 1) * self.width]:
                rows.append(160 if value < 0 else round(255 * (1 - value / 100)))
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b"")
        )
