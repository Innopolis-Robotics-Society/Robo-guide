"""ROS adapter persistence, announcement and final velocity interlock."""

import json
import time
from types import SimpleNamespace

import rclpy
from rclpy.parameter import Parameter
from std_msgs.msg import String
from geometry_msgs.msg import Twist
from guide_robot_navigation.localization_guard import LocalizationGuard, VelocityGate


def test_loss_persists_and_speech_is_attempted_once(tmp_path):
    context = rclpy.Context()
    rclpy.init(context=context)
    path = tmp_path / "lost"
    node = LocalizationGuard(
        context=context, parameter_overrides=[Parameter("latch_file", value=str(path))]
    )
    calls = []
    node.say = SimpleNamespace(server_is_ready=lambda: calls.append("check") or False)
    try:
        node.policy.lose("uncertainty_exceeded")
        node.tick()
        node.tick()
        assert path.exists()
        assert calls == ["check"]
    finally:
        node.destroy_node()
    second = LocalizationGuard(
        context=context, parameter_overrides=[Parameter("latch_file", value=str(path))]
    )
    try:
        assert second.policy.latched and second.policy.state == "lost"
    finally:
        second.destroy_node()
        context.shutdown()


def test_final_gate_rejects_old_heartbeat_and_administrative_velocity():
    messages = []
    node = SimpleNamespace(allowed_until=0.0, pub=SimpleNamespace(publish=messages.append))
    node.tick = lambda: VelocityGate.tick(node)
    command = Twist()
    command.linear.x = 1.0
    VelocityGate.command(node, command)
    assert messages[-1].linear.x == 0
    VelocityGate.status(
        node,
        String(data=json.dumps(dict(motion_blocked=False, heartbeat_monotonic=time.monotonic()))),
    )
    VelocityGate.command(node, command)
    assert messages[-1].linear.x == 1
    VelocityGate.status(
        node,
        String(
            data=json.dumps(dict(motion_blocked=False, heartbeat_monotonic=time.monotonic() - 1))
        ),
    )
    VelocityGate.command(node, command)
    assert messages[-1].linear.x == 0


def test_idle_refresh_interval_and_recovery_polling(tmp_path, monkeypatch):
    from concurrent.futures import Future
    from guide_robot_navigation import localization_guard as module

    context = rclpy.Context()
    rclpy.init(context=context)
    node = LocalizationGuard(context=context, parameter_overrides=[
        Parameter('latch_file', value=str(tmp_path / 'lost'))])
    now = [1000.0]
    calls = []
    def request(_):
        calls.append(now[0])
        result = Future()
        result.set_result(None)
        return result
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    node.update = SimpleNamespace(service_is_ready=lambda: True, call_async=request)
    node.active = SimpleNamespace(service_is_ready=lambda: False)
    node.tf_ok = lambda: True
    node.announced = True
    node.policy.lose('operator_reset_required')
    try:
        node.tick()
        now[0] += 299
        node.tick()
        assert len(calls) == 1
        now[0] += 1
        node.tick()
        assert len(calls) == 2
        node.policy.reset(now[0])
        now[0] += .5
        node.tick()
        assert len(calls) == 3
        now[0] += .5
        node.tick()
        assert len(calls) == 4
        # An explicit refresh cannot acknowledge a loss/recovery.
        reply = node.refresh(None, SimpleNamespace(success=None))
        assert not reply.success
        node.policy.state = 'ready'
        node.policy.latched = False
        node.active_ok = True
        node.active_at = now[0]
        reply = node.refresh(None, SimpleNamespace(success=None))
        assert reply.success
        node.tick()
        assert len(calls) == 5
    finally:
        node.destroy_node()
        context.shutdown()
