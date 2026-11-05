"""Actual aggregator override policy with controlled monotonic time (no ROS graph)."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from guide_robot_face.aggregator_node import FaceAggregatorNode
from guide_robot_face.lib.aggregator import FaceInputs


def node():
    n = FaceAggregatorNode.__new__(FaceAggregatorNode)
    n._inp = FaceInputs(presence=True)
    n._override = None
    n._expressions = {"idle", "happy", "sleep"}
    n._publish_if_changed = lambda: None
    return n


def test_expiry_clear_and_safety_priority(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("guide_robot_face.aggregator_node.time.monotonic", lambda: clock[0])
    n = node()
    req = SimpleNamespace(state="happy", gaze_az=20, duration_s=5)
    response = n._set_override(req, SimpleNamespace())
    assert response.accepted and response.source == "override"
    assert n._effective() == ("happy", 20, "override")
    n._inp = replace(n._inp, navigating=True)
    assert n._effective() == ("driving", 0, "automatic")
    n._inp = replace(n._inp, supervisor_fault=True)
    assert n._effective() == ("error", 0, "automatic")
    clock[0] += 6
    n._inp = FaceInputs(presence=True)
    assert n._effective() == ("idle", 0, "automatic")
    n._set_override(req, SimpleNamespace())
    req.state = ""
    n._set_override(req, SimpleNamespace())
    assert n._effective() == ("idle", 0, "automatic")


@pytest.mark.parametrize(
    "state,gaze,duration",
    [
        ("missing", 0, 1),
        ("happy", 0, 0),
        ("happy", 91, 1),
        ("happy", 0, float("nan")),
        ("happy", 0, 61),
    ],
)
def test_invalid_override(state, gaze, duration):
    n = node()
    response = n._set_override(
        SimpleNamespace(state=state, gaze_az=gaze, duration_s=duration), SimpleNamespace()
    )
    assert not response.accepted and n._override is None


def test_answer_during_navigation_keeps_driving_priority():
    from guide_robot_msgs.msg import MissionState

    n = node()
    n._set_override(SimpleNamespace(state="happy", gaze_az=20, duration_s=5), SimpleNamespace())
    n._on_mission(
        MissionState(state=MissionState.STATE_ANSWERING, base_state=MissionState.STATE_NAVIGATING)
    )
    assert n._effective() == ("driving", 0, "automatic")
    n._on_mission(
        MissionState(state=MissionState.STATE_ANSWERING, base_state=MissionState.STATE_NARRATING)
    )
    assert n._effective() == ("happy", 20, "override")


def test_supervisor_volatile_publication_reaches_face():
    import time

    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from std_msgs.msg import String

    rclpy.init()
    face = FaceAggregatorNode()
    supervisor = Node("test_face_supervisor")
    publisher = supervisor.create_publisher(String, "/supervisor/state", 10)
    executor = SingleThreadedExecutor()
    executor.add_node(face)
    executor.add_node(supervisor)
    try:
        deadline = time.monotonic() + 3
        while not face._supervisor_fault and time.monotonic() < deadline:
            publisher.publish(String(data="FAULT"))
            executor.spin_once(timeout_sec=0.05)
        assert face._effective() == ("error", 0, "automatic")
    finally:
        executor.shutdown()
        face.destroy_node()
        supervisor.destroy_node()
        rclpy.try_shutdown()
