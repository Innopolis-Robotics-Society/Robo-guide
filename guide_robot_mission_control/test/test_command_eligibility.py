"""ROS commands must reject wrong states instead of queueing work for a future state."""

import threading
from types import SimpleNamespace

import pytest
from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import MissionState
from guide_robot_msgs.srv import SubmitAnswer
from rclpy.action import GoalResponse
from std_srvs.srv import SetBool, Trigger

from guide_robot_mission_control.mission_fsm_node import MissionFsmNode, available_commands

STATES = [getattr(MissionState, key) for key in dir(MissionState) if key.startswith("STATE_")]
COMMANDS = [
    ("missions/pause", "_srv_request_pause", Trigger),
    ("missions/resume", "_srv_request_resume", Trigger),
    ("interaction/confirm", "_srv_submit_confirm", SetBool),
    ("interaction/answer", "_srv_submit_answer", SubmitAnswer),
]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("command,method,service_type", COMMANDS)
def test_commands_reject_ineligible_state(state, command, method, service_type):
    node = MissionFsmNode.__new__(MissionFsmNode)
    node._exec_lock = threading.Lock()
    node._state_lock = threading.Lock()
    node._last_state_msg = MissionState(state=state)
    calls = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    node._active_ctx = SimpleNamespace(
        request_pause=record, request_resume=record, submit_confirm=record, submit_answer=record
    )
    response = getattr(node, method)(service_type.Request(), service_type.Response())
    accepted = getattr(response, "success", getattr(response, "accepted", False))
    expected = {
        "missions/pause": {
            MissionState.STATE_NAVIGATING,
            MissionState.STATE_NARRATING,
            MissionState.STATE_ANSWERING,
        },
        "missions/resume": {MissionState.STATE_PAUSED},
        "interaction/confirm": {MissionState.STATE_AWAITING_CONFIRM},
        "interaction/answer": {MissionState.STATE_ANSWERING},
    }
    assert accepted == (state in expected[command])
    assert (command in available_commands(state)) == accepted
    assert bool(calls) == accepted
    node._active_ctx = None
    response = getattr(node, method)(service_type.Request(), service_type.Response())
    assert not getattr(response, "success", getattr(response, "accepted", False))


def test_goal_reservation_blocks_second_goal_return_and_idle_check():

    node = MissionFsmNode.__new__(MissionFsmNode)
    node._exec_lock = threading.Lock()
    node._state_lock = threading.Lock()
    node.localization_blocked = lambda: False
    node._active = True
    node._active_ctx = None
    node._active_goal_handle = None
    node._goal_reserved = False
    node._standalone_return_active = False
    node._last_state_msg = MissionState(state=MissionState.STATE_IDLE)
    node.get_logger = lambda: SimpleNamespace(info=lambda _: None, warning=lambda _: None)
    assert node._srv_check_idle(Trigger.Request(), Trigger.Response()).success
    assert node._on_run_tour_goal(RunTour.Goal()) == GoalResponse.ACCEPT
    assert node._on_run_tour_goal(RunTour.Goal()) == GoalResponse.REJECT
    assert not node._srv_check_idle(Trigger.Request(), Trigger.Response()).success
    assert not node._srv_go_home(Trigger.Request(), Trigger.Response()).success
    assert node._srv_request_stop(Trigger.Request(), Trigger.Response()).success
    assert node._pending_stop_requested


@pytest.mark.parametrize("stops,index", [(["missing"], 0), (["known"], 1), ([], 0)])
def test_invalid_route_cannot_start_motion(stops, index):
    node = MissionFsmNode.__new__(MissionFsmNode)
    node._resolve_tour = lambda _: (stops, stops, "")
    node._fetch_locations = lambda: {"known": object()}
    node._finish_run_tour = lambda *args: args[-1]
    goal = RunTour.Goal(location_ids=stops, start_index=index)
    assert (
        node._execute_reserved_tour(SimpleNamespace(request=goal))
        == "invalid_route_or_start_index"
    )
