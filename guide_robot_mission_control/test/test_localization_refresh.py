"""Stationary localization refresh must never bypass the motion interlock."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from guide_robot_mission_control import mission_fsm_node as module


@pytest.mark.parametrize('state,fresh,blocked', [
    ('lost', True, True), ('recovering', False, True), ('ready', True, False)])
def test_only_stale_ready_estimates_request_refresh(monkeypatch, state, fresh, blocked):
    monkeypatch.setattr(module.time, 'monotonic', lambda: 100.)
    client = Mock()
    node = SimpleNamespace(
        get_parameter=lambda _: SimpleNamespace(value=True),
        _localization_received=100.,
        _localization_status=dict(state=state, fresh=fresh, motion_blocked=blocked),
        _localization_refresh=client)
    assert module.MissionFsmNode.localization_blocked(node) == blocked
    client.call_async.assert_not_called()


@pytest.mark.parametrize('ack,fresh_after,expected', [
    (True, True, False), (True, False, True), (False, False, True)])
def test_refresh_requires_observed_fresh_state(monkeypatch, ack, fresh_after, expected):
    now = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda dt: now.__setitem__(0, now[0]+dt))
    monkeypatch.setattr(module.rclpy, 'ok', lambda **kw: True)
    future = SimpleNamespace(result=lambda: SimpleNamespace(success=ack))
    node = SimpleNamespace(
        context=None, get_parameter=lambda _: SimpleNamespace(value=True),
        _localization_received=100.,
        _localization_status=dict(state='ready', fresh=False, motion_blocked=True),
        _localization_refresh=SimpleNamespace(service_is_ready=lambda: True,
                                             call_async=lambda _: future))
    def wait(*args):
        if fresh_after:
            node._localization_status.update(fresh=True, motion_blocked=False)
        return True
    monkeypatch.setattr(module, '_wait_future', wait)
    assert module.MissionFsmNode.localization_blocked(node) == expected
