"""build_snapshot(): чистая логика, без ROS (llm_plam.md §5)."""

from __future__ import annotations

from dataclasses import dataclass

from guide_robot_llm.snapshot import build_snapshot

_STATE_IDLE = 0
_STATE_NARRATING = 3
_STATE_ANSWERING = 4
_IRQ_NONE = 0
_IRQ_ANSWERING = 1


@dataclass
class _MissionState:
    state: int = _STATE_IDLE
    interrupt: int = _IRQ_NONE
    base_state: int = _STATE_IDLE
    tour_id: str = ""
    stop_index: int = 0
    stop_total: int = 0
    stop_id: str = ""
    resume_available: bool = False


@dataclass
class _Presence:
    present: bool = False
    seconds_since_evidence: float = float("inf")


def test_idle_snapshot_has_no_tour_fields() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=["start_tour"])
    assert snap["mission"] == {"state": "IDLE"}
    assert snap["tools_allowed"] == ["start_tour"]


def test_narrating_snapshot_reports_1_indexed_stop_and_zone() -> None:
    mission = _MissionState(
        state=_STATE_NARRATING,
        tour_id="hall_a",
        stop_index=2,
        stop_total=7,
        stop_id="dinosaurs",
    )
    snap = build_snapshot(
        mission, _Presence(present=True, seconds_since_evidence=4.2),
        tools_allowed=["say", "pause"], location_zone="hall_2",
    )
    assert snap["mission"] == {
        "state": "NARRATING",
        "tour": "hall_a",
        "stop": 3,  # index 2 -> человеку показываем "3 из 7"
        "of": 7,
        "location": "dinosaurs",
        "zone": "hall_2",
    }
    assert snap["presence"] == {"present": True, "last_evidence_s": 4.2}


def test_answering_snapshot_includes_interrupt_frame() -> None:
    mission = _MissionState(
        state=_STATE_ANSWERING, interrupt=_IRQ_ANSWERING, base_state=_STATE_NARRATING
    )
    snap = build_snapshot(mission, _Presence(), tools_allowed=["finish_answer"])
    assert snap["mission"]["interrupt"] == {"kind": "answer", "base": "NARRATING"}


def test_presence_absent_reports_infinite_last_evidence_rounded() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[])
    assert snap["presence"]["present"] is False
    assert snap["presence"]["last_evidence_s"] == float("inf")
