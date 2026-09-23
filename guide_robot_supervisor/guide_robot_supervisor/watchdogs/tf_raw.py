"""Watchdog: a direct TF edge parent -> child is still being published on /tf.

Cheap replacement for TFWatchdog. TFWatchdog keeps a tf2 TransformListener,
which deserializes every /tf message into Python objects (~75 Hz on the
robot: diff_drive odom, AMCL, robot_state_publisher) only so that two
watchdogs can read one stamp each. Here /tf is subscribed with raw=True and
the callback does a byte search for the two CDR-encoded frame names -- no
message objects are built at all.

A CDR string is a little-endian uint32 length (including the trailing NUL)
followed by the bytes and a NUL, so `<len>map\\0` cannot match inside
`<len>my_map\\0`. The edge counts as seen when a message contains both the
parent and the child string: diff_drive publishes {odom, base_link}, AMCL
{map, odom}, so odom -> base_link and map -> odom never alias each other.

Differences from TFWatchdog, on purpose:
- only a *direct* edge, published on /tf (not /tf_static, not a chain);
- age = time since the edge was last *received*, not now - header.stamp.
  AMCL future-dates its stamp by transform_tolerance, so receive time is
  the more honest "is it alive" signal anyway.
"""

from __future__ import annotations

import struct

from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from tf2_msgs.msg import TFMessage

from guide_robot_supervisor.watchdogs.base import Level, Status, WatchdogBase


def _cdr_string(text: str) -> bytes:
    data = text.encode() + b"\x00"
    return struct.pack("<I", len(data)) + data


class TFRawWatchdog(WatchdogBase):
    """Params:

    parent:     str    — e.g. "map"
    child:      str    — e.g. "odom"
    max_age:    float  — seconds without the edge before it counts as STALE
    grace:      float  — seconds after start before failing
    """

    def setup(self) -> None:
        self._parent = str(self.p("parent", "map")).lstrip("/")
        self._child = str(self.p("child", "base_link")).lstrip("/")
        self._max_age = float(self.p("max_age", 2.0))
        self._grace = float(self.p("grace", 15.0))
        self._t0 = self.now()
        self._last_seen: float | None = None
        self._parent_pat = _cdr_string(self._parent)
        self._child_pat = _cdr_string(self._child)
        # same QoS as tf2_ros.TransformListener for /tf
        qos = QoSProfile(
            depth=100,
            durability=DurabilityPolicy.VOLATILE,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._sub = self.node.create_subscription(
            TFMessage, "/tf", self._on_tf, qos,
            callback_group=self.node.cb_group, raw=True,
        )

    def _on_tf(self, data: bytes) -> None:
        if self._child_pat in data and self._parent_pat in data:
            self._last_seen = self.now()

    def check(self) -> Status:
        chain = f"{self._parent} -> {self._child}"
        if self._last_seen is None:
            level = Level.WARN if self.now() - self._t0 < self._grace else Level.ERROR
            return Status(level, f"{chain} unavailable: never seen on /tf")

        age = self.now() - self._last_seen
        values = {"age": f"{age:.2f} s"}
        if age > self._max_age:
            return Status(Level.STALE, f"{chain} stale ({age:.1f} s)", values)
        return Status(Level.OK, chain, values)

    def reset(self) -> None:
        self._t0 = self.now()
        self._last_seen = None

    def destroy(self) -> None:
        self.node.destroy_subscription(self._sub)
