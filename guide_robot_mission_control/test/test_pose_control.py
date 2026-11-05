"""Pose commands use the same ownership slot as tours and localization."""

import threading
from types import SimpleNamespace

import pytest
from builtin_interfaces.msg import Time
from geometry_msgs.msg import PoseStamped
from guide_robot_msgs.action import GoToPose
from guide_robot_msgs.srv import ResetLocalization
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient

from guide_robot_mission_control.lib.map_geometry import OccupancyMap
from guide_robot_mission_control.pose_control import PoseControl
from test.mission_fsm_test_helpers import make_fsm_node, pump_clock
from test.mocks.harness import MissionTestHarness, wait_for_future, wait_until


def test_reservation_excludes_every_other_owner():
    control = PoseControl.__new__(PoseControl)
    node = SimpleNamespace(
        _active=True, _active_ctx=None, _goal_reserved=False, _safety_hold_event=threading.Event()
    )
    control.owner = node
    assert control.reserve()
    assert not control.reserve()
    node._goal_reserved = False
    node._active_ctx = object()
    assert not control.reserve()
    node._active_ctx = None
    node._safety_hold_event.set()
    assert not control.reserve()


@pytest.mark.parametrize("cancel,failed,lost", [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_pose_action_runs_existing_navigation_fsm(cancel, failed, lost):

    harness = MissionTestHarness()
    try:
        node = make_fsm_node(harness)
        if failed:
            harness.nav.mode = harness.nav.MODE_ABORT
        grid = OccupancyGrid()
        grid.header.frame_id = "map"
        grid.info.width = grid.info.height = 10
        grid.info.resolution = 1.0
        grid.info.origin.orientation.w = 1.0
        grid.data = [0] * 100
        node._pose_control.map = OccupancyMap(grid)
        node._pose_control.buffer.lookup_transform = lambda *a: SimpleNamespace(
            header=SimpleNamespace(stamp=node.get_clock().now().to_msg())
        )
        client_node = harness.make_client_node("pose_test_client")
        client = ActionClient(client_node, GoToPose, "/mission_fsm/go_to_pose")
        assert client.wait_for_server(timeout_sec=3)
        wait_until(node._nav_client.server_is_ready)
        wait_until(lambda: not node.localization_blocked())
        pose = PoseStamped()
        pose.header.frame_id = "map"
        pose.pose.position.x = pose.pose.position.y = 2.0
        pose.pose.orientation.w = 1.0
        pending = client.send_goal_async(
            GoToPose.Goal(map_version=node._pose_control.map.version, pose=pose)
        )
        wait_for_future(pending)
        handle = pending.result()
        assert handle.accepted
        if lost:
            wait_until(lambda: harness.nav.goals_received == 1)
            harness.localization_status = '{"state":"lost","motion_blocked":true}'
            wait_until(node.localization_blocked)
        if cancel:
            wait_until(lambda: harness.nav.goals_received == 1)
            second = client.send_goal_async(
                GoToPose.Goal(map_version=node._pose_control.map.version, pose=pose)
            )
            wait_for_future(second)
            assert not second.result().accepted
            wait_for_future(handle.cancel_goal_async())
        result = handle.get_result_async()
        pump_clock(harness, result.done, step=0.1, max_iterations=200)
        wait_for_future(result)
        assert result.result().result.outcome == (1 if cancel else 2 if failed or lost else 0)
        if lost:
            assert result.result().result.detail == "localization_lost"
        assert harness.nav.goals_received == 1
        assert not node._goal_reserved
    finally:
        harness.shutdown()


def test_reset_holds_ownership_and_reports_partial_completion():

    control = PoseControl.__new__(PoseControl)
    control.map_lock = threading.Lock()
    published = []
    owner = SimpleNamespace(
        _exec_lock=threading.Lock(),
        _active=True,
        _active_ctx=None,
        _goal_reserved=False,
        _safety_hold_event=threading.Event(),
        context=None,
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=Time)),
        get_parameter=lambda name: SimpleNamespace(
            value=False if name == "require_localization" else 0.0 if name == "initialpose_settle_s" else [0.25, 0.25, 0.06]
        ),
    )
    control.owner = owner
    control.validate = lambda *args, **kwargs: None

    def publish(msg):
        assert owner._goal_reserved
        published.append(msg)

    control.publisher = SimpleNamespace(publish=publish)
    control.clearers = [SimpleNamespace(service_is_ready=lambda: False)]
    response = control.reset(
        ResetLocalization.Request(pose=PoseStamped()), ResetLocalization.Response()
    )
    assert response.accepted and not response.costmaps_cleared
    assert len(published) == 1 and not owner._goal_reserved

    def failed_publish(msg):
        raise RuntimeError("publisher unavailable")

    control.publisher.publish = failed_publish
    with pytest.raises(RuntimeError, match="publisher unavailable"):
        control.reset(ResetLocalization.Request(pose=PoseStamped()), ResetLocalization.Response())
    assert not owner._goal_reserved
