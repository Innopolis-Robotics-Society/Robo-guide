#!/usr/bin/env python3
"""Robo-guide supervisor: ordered lifecycle bring-up + pluggable watchdogs.

Responsibilities
----------------
1. Bring up ``nav2_lifecycle_manager`` groups in a declared dependency order
   (safety -> localization -> navigation), gating each group behind
   preconditions (sensors publishing, TF present, ...).
2. Run watchdogs continuously afterwards and apply a per-watchdog policy
   (warn / pause / reset / shutdown / estop) with debounce and auto-recovery.
3. Expose the aggregate state on /supervisor/state and /diagnostics, and
   manual control via ~/bringup, ~/shutdown, ~/reset services.

All lifecycle managers must be launched with ``autostart: false`` — the
supervisor owns the ordering.
"""

from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import rclpy
import yaml
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from nav2_msgs.srv import ManageLifecycleNodes
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger

from guide_robot_supervisor.watchdogs.base import Level, Status

MANAGE = ManageLifecycleNodes.Request


class SupervisorState(str, Enum):
    INIT = "INIT"
    BRINGUP = "BRINGUP"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAULT = "FAULT"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class Group:
    name: str
    manager: str
    requires: List[str] = field(default_factory=list)
    preconditions: List[str] = field(default_factory=list)
    startup_timeout: float = 30.0
    precondition_timeout: float = 60.0
    optional: bool = False
    state: str = "INACTIVE"
    client: object = None


@dataclass
class Policy:
    on_error: str = "warn"          # warn | pause | reset | shutdown | estop
    target: Optional[str] = None    # group name; None -> all groups
    debounce: int = 3               # consecutive bad checks before acting
    recover: bool = True            # resume automatically when OK returns
    period: float = 1.0             # check period, seconds


@dataclass
class WatchdogSlot:
    name: str
    impl: object
    policy: Policy
    status: Status = field(default_factory=lambda: Status(Level.OK, "not run yet"))
    strikes: int = 0
    latched: bool = False
    next_check: float = 0.0


class Supervisor(Node):
    def __init__(self) -> None:
        super().__init__("supervisor")
        self.cb_group = ReentrantCallbackGroup()

        self.declare_parameter("config_file", "")
        self.declare_parameter("autostart", True)
        self.declare_parameter("loop_period", 0.2)
        self.declare_parameter("estop_topic", "/supervisor/estop")

        cfg_path = self.get_parameter("config_file").value
        if not cfg_path:
            raise RuntimeError("parameter 'config_file' is required")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)["supervisor"]

        self._period = float(self.get_parameter("loop_period").value)
        self._autostart = bool(self.get_parameter("autostart").value)
        self._state = SupervisorState.INIT
        self._lock = threading.RLock()
        self._stop = threading.Event()

        self.groups: Dict[str, Group] = {}
        self.order: List[str] = []
        for g in cfg.get("groups", []):
            grp = Group(**g)
            grp.client = self.create_client(
                ManageLifecycleNodes,
                f"{grp.manager}/manage_nodes",
                callback_group=self.cb_group,
            )
            self.groups[grp.name] = grp
            self.order.append(grp.name)

        self.watchdogs: Dict[str, WatchdogSlot] = {}
        for w in cfg.get("watchdogs", []):
            self._add_watchdog(w)

        # interfaces
        self._state_pub = self.create_publisher(String, "~/state", 10)
        self._diag_pub = self.create_publisher(DiagnosticArray, "~/diagnostics", 10)
        self._estop_pub = self.create_publisher(
            Bool, self.get_parameter("estop_topic").value, 10
        )
        self.create_service(Trigger, "~/bringup", self._srv_bringup,
                            callback_group=self.cb_group)
        self.create_service(Trigger, "~/shutdown", self._srv_shutdown,
                            callback_group=self.cb_group)
        self.create_service(Trigger, "~/reset", self._srv_reset,
                            callback_group=self.cb_group)
        self.create_timer(1.0, self._publish_diagnostics, callback_group=self.cb_group)

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self.get_logger().info(
            f"supervisor up: groups={self.order}, watchdogs={list(self.watchdogs)}"
        )

    # ------------------------------------------------------------------ setup
    def _add_watchdog(self, spec: dict) -> None:
        name = spec["name"]
        module_path, _, cls_name = spec["class"].rpartition(".")
        cls = getattr(importlib.import_module(module_path), cls_name)
        impl = cls(name, self, spec.get("params", {}))
        self.watchdogs[name] = WatchdogSlot(
            name=name, impl=impl, policy=Policy(**spec.get("policy", {}))
        )

    # -------------------------------------------------------------- utilities
    def call_sync(self, client, request, timeout: float = 5.0):
        """Blocking service call, safe from the worker thread.

        Requires a MultiThreadedExecutor spinning this node elsewhere.
        """
        if not client.service_is_ready() and not client.wait_for_service(timeout_sec=timeout):
            return None
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline and rclpy.ok():
            time.sleep(0.02)
        if not future.done():
            future.cancel()
            return None
        return future.result()

    def _set_state(self, state: SupervisorState) -> None:
        if self._state != state:
            self.get_logger().info(f"state: {self._state.value} -> {state.value}")
            self._state = state
        self._state_pub.publish(String(data=self._state.value))

    def _manage(self, group: Group, command: int, timeout: float) -> bool:
        req = ManageLifecycleNodes.Request(command=command)
        res = self.call_sync(group.client, req, timeout)
        ok = bool(res and res.success)
        if not ok:
            self.get_logger().error(
                f"[{group.name}] command {command} on {group.manager} failed"
            )
        return ok

    # ------------------------------------------------------------------- FSM
    def _run(self) -> None:
        while rclpy.ok() and not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # a supervisor must not die
                self.get_logger().error(f"tick failed: {exc!r}")
            time.sleep(self._period)

    def _tick(self) -> None:
        self._poll_watchdogs()
        with self._lock:
            state = self._state

        if state is SupervisorState.INIT:
            if self._autostart:
                self._bringup()
        elif state in (SupervisorState.ACTIVE, SupervisorState.DEGRADED):
            self._apply_policies()
        self._state_pub.publish(String(data=self._state.value))

    # --------------------------------------------------------------- bring-up
    def _wait_preconditions(self, group: Group) -> bool:
        if not group.preconditions:
            return True
        deadline = time.monotonic() + group.precondition_timeout
        while time.monotonic() < deadline and rclpy.ok() and not self._stop.is_set():
            self._poll_watchdogs(force=True)
            bad = [
                n for n in group.preconditions
                if n in self.watchdogs and self.watchdogs[n].status.level != Level.OK
            ]
            if not bad:
                return True
            time.sleep(0.5)
        self.get_logger().error(f"[{group.name}] preconditions not met: {bad}")
        return False

    def _bringup(self) -> bool:
        self._set_state(SupervisorState.BRINGUP)
        for name in self.order:
            grp = self.groups[name]
            unmet = [r for r in grp.requires if self.groups[r].state != "ACTIVE"]
            if unmet:
                self.get_logger().error(f"[{name}] dependencies not active: {unmet}")
                if not grp.optional:
                    self._set_state(SupervisorState.FAULT)
                    return False
                continue

            if not self._wait_preconditions(grp):
                if grp.optional:
                    continue
                self._set_state(SupervisorState.FAULT)
                return False

            self.get_logger().info(f"[{name}] STARTUP -> {grp.manager}")
            if self._manage(grp, MANAGE.STARTUP, grp.startup_timeout):
                grp.state = "ACTIVE"
            else:
                grp.state = "FAILED"
                if not grp.optional:
                    self._set_state(SupervisorState.FAULT)
                    return False

        self._estop_pub.publish(Bool(data=False))
        self._set_state(SupervisorState.ACTIVE)
        return True

    def _shutdown_all(self) -> None:
        self._set_state(SupervisorState.SHUTDOWN)
        for name in reversed(self.order):
            grp = self.groups[name]
            if grp.state == "ACTIVE":
                self._manage(grp, MANAGE.SHUTDOWN, 10.0)
                grp.state = "INACTIVE"

    # -------------------------------------------------------------- watchdogs
    def _poll_watchdogs(self, force: bool = False) -> None:
        now = time.monotonic()
        for slot in self.watchdogs.values():
            if not force and now < slot.next_check:
                continue
            slot.next_check = now + slot.policy.period
            try:
                slot.status = slot.impl.check()
            except Exception as exc:
                slot.status = Status(Level.ERROR, f"check raised {exc!r}")
            if slot.status.bad:
                slot.strikes += 1
            else:
                slot.strikes = 0

    def _apply_policies(self) -> None:
        degraded = False
        for slot in self.watchdogs.values():
            pol = slot.policy
            tripped = slot.strikes >= pol.debounce

            if tripped and not slot.latched:
                slot.latched = True
                self.get_logger().error(
                    f"watchdog '{slot.name}' tripped ({slot.status.message}) "
                    f"-> action '{pol.on_error}'"
                )
                self._do_action(pol)
            elif not tripped and slot.latched and pol.recover and slot.status.level == Level.OK:
                slot.latched = False
                self.get_logger().info(f"watchdog '{slot.name}' recovered -> resuming")
                slot.impl.reset()
                self._undo_action(pol)

            degraded = degraded or slot.latched

        if self._state is not SupervisorState.FAULT:
            self._set_state(
                SupervisorState.DEGRADED if degraded else SupervisorState.ACTIVE
            )

    def _targets(self, pol: Policy) -> List[Group]:
        if pol.target:
            return [self.groups[pol.target]] if pol.target in self.groups else []
        return [self.groups[n] for n in reversed(self.order)]

    def _do_action(self, pol: Policy) -> None:
        action = pol.on_error
        if action == "warn":
            return
        if action == "estop":
            self._estop_pub.publish(Bool(data=True))
            return
        if action == "shutdown":
            self._estop_pub.publish(Bool(data=True))
            self._shutdown_all()
            self._set_state(SupervisorState.FAULT)
            return
        for grp in self._targets(pol):
            if grp.state != "ACTIVE":
                continue
            if action == "pause":
                self._manage(grp, MANAGE.PAUSE, 10.0)
                grp.state = "PAUSED"
            elif action == "reset":
                self._manage(grp, MANAGE.RESET, 15.0)
                grp.state = "INACTIVE"

    def _undo_action(self, pol: Policy) -> None:
        action = pol.on_error
        if action in ("warn",):
            return
        if action == "estop":
            if not any(s.latched for s in self.watchdogs.values()):
                self._estop_pub.publish(Bool(data=False))
            return
        self._set_state(SupervisorState.RECOVERING)
        for grp in self._targets(pol):
            if action == "pause" and grp.state == "PAUSED":
                self._manage(grp, MANAGE.RESUME, 10.0)
                grp.state = "ACTIVE"
            elif action == "reset" and grp.state == "INACTIVE":
                if self._manage(grp, MANAGE.STARTUP, grp.startup_timeout):
                    grp.state = "ACTIVE"

    # --------------------------------------------------------------- services
    def _srv_bringup(self, _req, res):
        ok = self._bringup()
        res.success, res.message = ok, self._state.value
        return res

    def _srv_shutdown(self, _req, res):
        self._shutdown_all()
        res.success, res.message = True, self._state.value
        return res

    def _srv_reset(self, _req, res):
        self._shutdown_all()
        for slot in self.watchdogs.values():
            slot.strikes, slot.latched = 0, False
            slot.impl.reset()
        self._set_state(SupervisorState.INIT)
        res.success, res.message = True, "reset"
        return res

    # ------------------------------------------------------------ diagnostics
    def _publish_diagnostics(self) -> None:
        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()

        top_level = (
            DiagnosticStatus.OK if self._state is SupervisorState.ACTIVE
            else DiagnosticStatus.ERROR if self._state is SupervisorState.FAULT
            else DiagnosticStatus.WARN
        )
        top = DiagnosticStatus(
            name="supervisor", hardware_id="robo-guide",
            level=top_level,
            message=self._state.value,
        )
        top.values = [KeyValue(key=g.name, value=g.state) for g in self.groups.values()]
        msg.status.append(top)

        for slot in self.watchdogs.values():
            st = DiagnosticStatus(
                name=f"supervisor/{slot.name}",
                hardware_id="robo-guide",
                level=bytes([int(slot.status.level)]),
                message=slot.status.message,
            )
            st.values = [KeyValue(key=k, value=str(v))
                         for k, v in slot.status.values.items()]
            st.values.append(KeyValue(key="strikes", value=str(slot.strikes)))
            st.values.append(KeyValue(key="latched", value=str(slot.latched)))
            msg.status.append(st)

        self._diag_pub.publish(msg)

    def destroy_node(self) -> bool:
        self._stop.set()
        for slot in self.watchdogs.values():
            slot.impl.destroy()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = Supervisor()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()