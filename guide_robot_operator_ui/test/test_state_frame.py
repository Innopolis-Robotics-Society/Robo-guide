"""Юниты lib/state_frame.py -- без rclpy, без ROS-графа (design C4)."""

from __future__ import annotations

from guide_robot_operator_ui.lib.state_frame import STATE_NAMES, build_frame

from guide_robot_msgs.msg import MissionState


def test_state_names_covers_every_mission_state_constant() -> None:
    """Расхождение STATE_NAMES <-> MissionState.msg ловится сборкой, не глазами (design C4)."""
    declared = {
        getattr(MissionState, name) for name in dir(MissionState) if name.startswith("STATE_")
    }
    assert set(STATE_NAMES.keys()) == declared


def test_build_frame_before_first_mission_state() -> None:
    frame = build_frame(
        seq=1,
        mission_msg=None,
        estop=False,
        supervisor_state="",
        now_s=100.0,
        mission_received_at_s=None,
    )
    assert frame["state"] == -1
    assert frame["state_name"] == "unknown"
    assert frame["mission_state_age_s"] is None
    assert frame["stop_total"] == 0
    assert frame["tour_id"] == ""


def _mission_msg(**overrides: object) -> MissionState:
    msg = MissionState()
    msg.state = MissionState.STATE_NARRATING
    msg.tour_id = "expo_short"
    msg.stop_index = 2
    msg.stop_total = 5
    msg.stop_id = "expo_city_model"
    msg.exhibit_id = "expo_city_model"
    msg.next_exhibit_id = "expo_handoff"
    msg.chunk_index = 1
    msg.chunk_total = 3
    msg.pause_reason = MissionState.PAUSE_NONE
    for key, value in overrides.items():
        setattr(msg, key, value)
    return msg


def test_build_frame_with_mission_state() -> None:
    frame = build_frame(
        seq=128,
        mission_msg=_mission_msg(),
        estop=False,
        supervisor_state="RUNNING",
        now_s=100.3,
        mission_received_at_s=100.0,
    )
    assert frame["seq"] == 128
    assert frame["state"] == MissionState.STATE_NARRATING
    assert frame["state_name"] == "narrating"
    assert frame["tour_id"] == "expo_short"
    assert frame["stop_index"] == 2
    assert frame["stop_total"] == 5
    assert frame["stop_id"] == "expo_city_model"
    assert frame["exhibit_id"] == "expo_city_model"
    assert frame["next_exhibit_id"] == "expo_handoff"
    assert frame["chunk_index"] == 1
    assert frame["chunk_total"] == 3
    assert frame["paused_reason"] == MissionState.PAUSE_NONE
    assert frame["estop"] is False
    assert frame["supervisor_state"] == "RUNNING"
    assert abs(frame["mission_state_age_s"] - 0.3) < 1e-9


def test_build_frame_age_never_negative() -> None:
    frame = build_frame(
        seq=1,
        mission_msg=_mission_msg(),
        estop=False,
        supervisor_state="",
        now_s=99.0,  # "раньше" момента получения -- часы могли скакнуть назад
        mission_received_at_s=100.0,
    )
    assert frame["mission_state_age_s"] == 0.0


def test_build_frame_unknown_state_code_maps_to_unknown_name() -> None:
    frame = build_frame(
        seq=1,
        mission_msg=_mission_msg(state=99),
        estop=False,
        supervisor_state="",
        now_s=1.0,
        mission_received_at_s=1.0,
    )
    assert frame["state_name"] == "unknown"
