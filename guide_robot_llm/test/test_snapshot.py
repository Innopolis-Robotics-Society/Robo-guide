"""build_snapshot(): чистая логика, без ROS (llm_plam.md §5)."""

from __future__ import annotations

from dataclasses import dataclass

from guide_robot_llm.snapshot import build_snapshot, render_status_line

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


def test_location_name_added_to_mission_section_when_given() -> None:
    mission = _MissionState(state=_STATE_NARRATING, stop_id="lab_demo")
    snap = build_snapshot(
        mission, _Presence(), tools_allowed=[], location_name="Демонстрационная лаборатория"
    )
    assert snap["mission"]["location_name"] == "Демонстрационная лаборатория"


def test_location_name_absent_by_default() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[])
    assert "location_name" not in snap["mission"]


def test_told_ids_become_already_told_key() -> None:
    snap = build_snapshot(
        _MissionState(), _Presence(), tools_allowed=[], told_ids=["lab_demo", "cafe"]
    )
    assert snap["already_told"] == ["lab_demo", "cafe"]


def test_told_ids_empty_omits_key() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[])
    assert "already_told" not in snap


def test_nearby_becomes_key_when_given() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[], nearby=["cafe", "exit"])
    assert snap["nearby"] == ["cafe", "exit"]


def test_nearby_empty_omits_key() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[])
    assert "nearby" not in snap


def test_pending_question_becomes_key_when_given() -> None:
    """stage2 C2: снимок для interaction_log отражает вопрос, на который отвечает ход."""
    snap = build_snapshot(
        _MissionState(), _Presence(), tools_allowed=[], pending_question="Идём к лидару?"
    )
    assert snap["pending_question"] == "Идём к лидару?"


def test_pending_question_absent_by_default() -> None:
    snap = build_snapshot(_MissionState(), _Presence(), tools_allowed=[])
    assert "pending_question" not in snap


# -- render_status_line: служебная строка [состояние: ...] последнего сообщения --


def test_status_line_idle_minimal() -> None:
    snap = build_snapshot(
        _MissionState(), _Presence(present=True, seconds_since_evidence=0.0), tools_allowed=[]
    )
    assert render_status_line(snap) == "[состояние: IDLE, посетитель рядом]"


def test_status_line_full_tour_state() -> None:
    snap = build_snapshot(
        _MissionState(
            state=_STATE_NARRATING, tour_id="obzor", stop_index=1, stop_total=5, stop_id="lab_demo"
        ),
        _Presence(present=True, seconds_since_evidence=0.0),
        tools_allowed=["stop_tour"],
        location_name="Лаборатория",
    )
    line = render_status_line(snap)
    assert line.startswith("[состояние: NARRATING")
    assert 'тур "obzor"' in line
    assert "остановка 2 из 5 — Лаборатория" in line
    assert line.endswith("посетитель рядом]")


def test_status_line_absent_visitor_reports_seconds() -> None:
    snap = build_snapshot(
        _MissionState(), _Presence(present=False, seconds_since_evidence=12.3), tools_allowed=[]
    )
    assert "посетителя не видно 12.3 с" in render_status_line(snap)


def test_status_line_interrupt_mentioned() -> None:
    snap = build_snapshot(
        _MissionState(
            state=_STATE_ANSWERING, interrupt=_IRQ_ANSWERING, base_state=_STATE_NARRATING
        ),
        _Presence(present=True, seconds_since_evidence=0.0),
        tools_allowed=[],
    )
    assert "рассказ прерван (answer)" in render_status_line(snap)


def test_status_line_omits_tools_and_told_ids() -> None:
    """Гейт инструментов уже в GBNF -- строка статуса его не дублирует."""
    snap = build_snapshot(
        _MissionState(),
        _Presence(present=True, seconds_since_evidence=0.0),
        tools_allowed=["start_tour", "guide_to"],
        told_ids=["lab_demo"],
    )
    line = render_status_line(snap)
    assert "start_tour" not in line
    assert "lab_demo" not in line
