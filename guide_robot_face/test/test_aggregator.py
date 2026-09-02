"""Маппинг FaceInputs → state. Без ROS."""

from guide_robot_face.lib.aggregator import (
    PHASE_ACTION,
    PHASE_ANSWER,
    PHASE_AWAITING,
    PHASE_IDLE,
    FaceInputs,
    decide,
)


def test_default_is_sleep() -> None:
    assert decide(FaceInputs()) == "sleep"


def test_presence_idle() -> None:
    assert decide(FaceInputs(presence=True)) == "idle"


def test_priority_order() -> None:
    full = FaceInputs(
        supervisor_fault=True,
        speaking=True,
        dialog_phase=PHASE_ACTION,
        navigating=True,
        vad_active=True,
        wakeword_hold=True,
        presence=True,
    )
    assert decide(full) == "error"
    assert decide(FaceInputs(speaking=True, dialog_phase=PHASE_ACTION, presence=True)) == (
        "speaking"
    )
    assert decide(FaceInputs(speaking=True, navigating=True, presence=True)) == "driving"
    assert decide(FaceInputs(dialog_phase=PHASE_ANSWER, navigating=True, presence=True)) == (
        "driving"
    )
    assert decide(FaceInputs(navigating=True, vad_active=True, presence=True)) == "driving"
    assert decide(FaceInputs(vad_active=True, presence=True)) == "listening"


def test_thinking_action_and_answer() -> None:
    assert decide(FaceInputs(dialog_phase=PHASE_ACTION, presence=True)) == "thinking"
    assert decide(FaceInputs(dialog_phase=PHASE_ANSWER, presence=True)) == "thinking"
    assert decide(FaceInputs(dialog_phase=PHASE_IDLE, presence=True)) == "idle"


def test_listening_cues() -> None:
    assert decide(FaceInputs(dialog_phase=PHASE_AWAITING, presence=True)) == "listening"
    assert decide(FaceInputs(wakeword_hold=True, presence=False)) == "listening"
    assert decide(FaceInputs(vad_active=True, presence=False)) == "listening"


def test_sleep_without_presence_when_idle_pipeline() -> None:
    assert decide(FaceInputs(dialog_phase=PHASE_IDLE, presence=False)) == "sleep"
