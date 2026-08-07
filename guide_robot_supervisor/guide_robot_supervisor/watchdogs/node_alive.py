"""Watchdogs: node presence in the graph, and lifecycle_manager activity."""

from __future__ import annotations

from typing import List

from std_srvs.srv import Trigger

from guide_robot_supervisor.watchdogs.base import Level, Status, WatchdogBase


class NodeAliveWatchdog(WatchdogBase):
    """Params:

    nodes: list[str] — fully qualified node names, e.g. ["/controller_manager"]
    grace: float
    """

    def setup(self) -> None:
        self._nodes: List[str] = list(self.p("nodes", []))
        self._grace = float(self.p("grace", 15.0))
        self._t0 = self.now()

    def check(self) -> Status:
        present = set()
        for name, ns in self.node.get_node_names_and_namespaces():
            fq = f"{ns.rstrip('/')}/{name}"
            present.add(fq if fq.startswith("/") else "/" + fq)
        missing = [n for n in self._nodes if n not in present]
        if missing:
            level = Level.WARN if self.now() - self._t0 < self._grace else Level.ERROR
            return Status(level, f"missing: {', '.join(missing)}")
        return Status(Level.OK, f"{len(self._nodes)} nodes present")

    def reset(self) -> None:
        self._t0 = self.now()


class LifecycleManagerActiveWatchdog(WatchdogBase):
    """Calls <manager>/is_active. Detects a bond-triggered collapse of a group.

    Params:
      manager: str   — e.g. "/lifecycle_manager_safety"
      timeout: float — service call timeout
    """

    def setup(self) -> None:
        self._manager = str(self.p("manager"))
        self._timeout = float(self.p("timeout", 2.0))
        self._cli = self.node.create_client(
            Trigger, f"{self._manager}/is_active", callback_group=self.node.cb_group
        )

    def check(self) -> Status:
        if not self._cli.service_is_ready():
            return Status(Level.WARN, f"{self._manager}/is_active not ready")
        res = self.node.call_sync(self._cli, Trigger.Request(), self._timeout)
        if res is None:
            return Status(Level.STALE, f"{self._manager} did not answer")
        if not res.success:
            return Status(Level.ERROR, f"{self._manager} inactive")
        return Status(Level.OK, f"{self._manager} active")

    def destroy(self) -> None:
        self.node.destroy_client(self._cli)
