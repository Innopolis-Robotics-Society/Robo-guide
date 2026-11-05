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


import pytest


@pytest.mark.parametrize('failure', [None, 'position', 'yaw', 'covariance', 'clear', 'missing', 'motion', 'tf', 'stale', 'frame'])
def test_startup_parking_check_restores_only_after_verified_pose(tmp_path, monkeypatch, failure):
    import math
    from concurrent.futures import Future
    from geometry_msgs.msg import PoseWithCovarianceStamped
    from rclpy.clock import ClockType
    from rclpy.time import Time
    from guide_robot_navigation import localization_guard as module

    now = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    context = rclpy.Context()
    rclpy.init(context=context)
    path = tmp_path / 'lost'
    path.write_text('operator_reset_required\n')
    node = LocalizationGuard(context=context, parameter_overrides=[
        Parameter('latch_file', value=str(path)),
        Parameter('startup_parking_check', value=True)])
    node.get_clock = lambda: SimpleNamespace(now=lambda: Time(
        seconds=now[0], clock_type=ClockType.ROS_TIME))
    node.tf_ok = lambda: failure != 'tf'
    node.active = node.update = SimpleNamespace(service_is_ready=lambda: False)
    cleared = []
    def clear(_):
        cleared.append(now[0])
        future = Future()
        if failure == 'clear':
            future.set_exception(RuntimeError('test failure'))
        else:
            future.set_result(SimpleNamespace())
        return future
    node.startup_clearers = [SimpleNamespace(service_is_ready=lambda: True, call_async=clear)] * 2
    node.active_ok = failure != 'missing'
    node.active_at = now[0]
    try:
        assert node.policy.latched and node.policy.state == 'initializing'
        if failure == 'motion':
            node.navigation_active = True
        node.tick()
        assert node.policy.state != 'ready'
        assert path.exists()
        for _ in range(32):
            now[0] += .5
            node.active_at = now[0]
            msg = PoseWithCovarianceStamped()
            msg.header.frame_id = 'odom' if failure == 'frame' else 'map'
            msg.header.stamp = Time(seconds=now[0] - (10 if failure == 'stale' else 0),
                                    clock_type=ClockType.ROS_TIME).to_msg()
            msg.pose.pose.position.x = 25.0 if failure == 'position' else 20.686
            msg.pose.pose.position.y = 1.027
            yaw = 1.0 if failure == 'yaw' else -.218
            msg.pose.pose.orientation.z = math.sin(yaw/2)
            msg.pose.pose.orientation.w = math.cos(yaw/2)
            for index in (0, 7, 35):
                msg.pose.covariance[index] = 9.0 if failure == 'covariance' else .01
            node.sample(msg)
            node.tick()
            if now[0] < 1002.5:
                assert node.policy.state != 'ready'
        if failure is None:
            assert node.policy.state == 'ready'
            assert not node.policy.latched and not path.exists()
            assert len(cleared) == 2
            # Successful boot cannot auto-clear a later localization loss.
            node.policy.lose('uncertainty_exceeded')
            node.tick()
            assert node.policy.state == 'lost' and path.exists()
            assert len(cleared) == 2
        else:
            now[0] += 100
            node.tick()
            assert node.policy.state == 'lost'
            assert node.policy.latched and path.exists()
            if failure != 'clear':
                assert not cleared
    finally:
        node.destroy_node()
        context.shutdown()
