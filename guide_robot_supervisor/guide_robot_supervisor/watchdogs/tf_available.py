"""Watchdog: a TF chain exists and is not stale."""

from __future__ import annotations

import rclpy
from tf2_ros import Buffer, TransformListener

from guide_robot_supervisor.watchdogs.base import Level, Status, WatchdogBase


class TFWatchdog(WatchdogBase):
    """Params:

    parent:     str    — e.g. "map"
    child:      str    — e.g. "base_link"
    max_age:    float  — seconds; transform older than this counts as STALE
    grace:      float  — seconds after start before failing
    """

    def setup(self) -> None:
        self._parent = str(self.p("parent", "map"))
        self._child = str(self.p("child", "base_link"))
        self._max_age = float(self.p("max_age", 2.0))
        self._grace = float(self.p("grace", 15.0))
        self._t0 = self.now()
        # one shared buffer per node keeps /tf subscriptions down
        if not hasattr(self.node, "_tf_buffer"):
            self.node._tf_buffer = Buffer()
            self.node._tf_listener = TransformListener(self.node._tf_buffer, self.node)
        self._buffer: Buffer = self.node._tf_buffer

    def check(self) -> Status:
        chain = f"{self._parent} -> {self._child}"
        try:
            tf = self._buffer.lookup_transform(
                self._parent, self._child, rclpy.time.Time()
            )
        except Exception as exc:  # tf2 raises several unrelated exception types
            level = Level.WARN if self.now() - self._t0 < self._grace else Level.ERROR
            return Status(level, f"{chain} unavailable: {type(exc).__name__}")

        age = self.now() - (tf.header.stamp.sec + tf.header.stamp.nanosec * 1e-9)
        values = {"age": f"{age:.2f} s"}
        if age > self._max_age:
            return Status(Level.STALE, f"{chain} stale ({age:.1f} s)", values)
        return Status(Level.OK, chain, values)

    def reset(self) -> None:
        self._t0 = self.now()
