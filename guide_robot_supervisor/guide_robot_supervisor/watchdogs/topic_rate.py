"""Watchdog: publication rate / liveness of one or more topics."""

from __future__ import annotations

from typing import Dict, List

from rclpy.qos import QoSProfile, qos_profile_sensor_data, qos_profile_system_default
from rosidl_runtime_py.utilities import get_message

from guide_robot_supervisor.watchdogs.base import Level, Status, WatchdogBase

_QOS: Dict[str, QoSProfile] = {
    "sensor": qos_profile_sensor_data,      # BEST_EFFORT — sonars, scans, imu
    "default": qos_profile_system_default,  # RELIABLE
}


class TopicRateWatchdog(WatchdogBase):
    """Params:

    topics:    list[str]  — topics to watch (all must be alive)
    msg_type:  str        — e.g. "sensor_msgs/msg/Range"; auto-detected if omitted
    qos:       str        — "sensor" (default) | "default"
    min_rate:  float      — Hz, below which the topic is considered dead
    warn_rate: float      — Hz, below which a WARN is raised (optional)
    window:    int        — number of samples used for the rate estimate
    grace:     float      — seconds after start before the watchdog can fail
    """

    def setup(self) -> None:
        self._topics: List[str] = list(self.p("topics", []))
        self._min_rate = float(self.p("min_rate", 1.0))
        self._warn_rate = float(self.p("warn_rate", self._min_rate * 2.0))
        self._window = int(self.p("window", 10))
        self._grace = float(self.p("grace", 10.0))
        self._t0 = self.now()
        self._stamps: Dict[str, List[float]] = {t: [] for t in self._topics}
        self._subs = []

        qos = _QOS.get(str(self.p("qos", "sensor")), qos_profile_sensor_data)
        for topic in self._topics:
            msg_type = self._resolve_type(topic)
            if msg_type is None:
                self.log.warn(f"type of {topic} is unknown yet, will retry lazily")
                continue
            self._subscribe(topic, msg_type, qos)

    # ------------------------------------------------------------------
    def _resolve_type(self, topic: str):
        type_str = self.p("msg_type")
        if not type_str:
            info = self.node.get_publishers_info_by_topic(topic)
            if not info:
                return None
            type_str = info[0].topic_type
        try:
            return get_message(type_str)
        except (ImportError, ValueError, AttributeError) as exc:
            self.log.error(f"cannot import {type_str}: {exc}")
            return None

    def _subscribe(self, topic, msg_type, qos) -> None:
        sub = self.node.create_subscription(
            msg_type, topic, lambda _m, t=topic: self._on_msg(t), qos,
            callback_group=self.node.cb_group,
        )
        self._subs.append((topic, sub))

    def _on_msg(self, topic: str) -> None:
        buf = self._stamps[topic]
        buf.append(self.now())
        if len(buf) > self._window:
            del buf[0]

    def _rate(self, topic: str) -> float:
        buf = self._stamps[topic]
        if len(buf) < 2:
            return 0.0
        span = buf[-1] - buf[0]
        if span <= 0.0:
            return 0.0
        # a stale buffer must decay, not report the historical rate
        idle = self.now() - buf[-1]
        if idle > 2.0 / max(self._min_rate, 1e-3):
            return 0.0
        return (len(buf) - 1) / span

    # ------------------------------------------------------------------
    def check(self) -> Status:
        # lazy (re)subscribe for topics whose type was not resolvable at startup
        subscribed = {t for t, _ in self._subs}
        qos = _QOS.get(str(self.p("qos", "sensor")), qos_profile_sensor_data)
        for topic in self._topics:
            if topic not in subscribed:
                mt = self._resolve_type(topic)
                if mt is not None:
                    self._subscribe(topic, mt, qos)

        values = {t: f"{self._rate(t):.2f} Hz" for t in self._topics}
        dead = [t for t in self._topics if self._rate(t) < self._min_rate]
        slow = [t for t in self._topics if self._min_rate <= self._rate(t) < self._warn_rate]

        if dead:
            if self.now() - self._t0 < self._grace:
                return Status(Level.WARN, f"waiting for {', '.join(dead)}", values)
            return Status(Level.ERROR, f"no data: {', '.join(dead)}", values)
        if slow:
            return Status(Level.WARN, f"low rate: {', '.join(slow)}", values)
        return Status(Level.OK, "all topics nominal", values)

    def reset(self) -> None:
        self._t0 = self.now()
        for buf in self._stamps.values():
            buf.clear()

    def destroy(self) -> None:
        for _, sub in self._subs:
            self.node.destroy_subscription(sub)
        self._subs.clear()
