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
