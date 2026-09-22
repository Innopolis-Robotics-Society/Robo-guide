"""Sample-indexed pre-roll и fencing решений input admission."""

from __future__ import annotations

import numpy as np
import pytest
import rclpy
from guide_robot_msgs.msg import AudioChunk, UtteranceControl, UtteranceEvent
from rclpy.parameter import Parameter

from guide_robot_voice.asr_node import AsrNode, _DecodeJob
from guide_robot_voice.lib.ring import IndexedAudioRing, RingBuffer


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> None:
        self.messages.append(message)


@pytest.fixture
def node():
    rclpy.init()
    instance = AsrNode()
    instance.set_parameters(
        [
            Parameter("session_managed_input", value=True),
            Parameter("pre_roll_ms", value=500.0),
        ]
    )
    instance._indexed_pre_roll = IndexedAudioRing(16_000, max_samples=8_000)
    instance._utterance_event_pub = _Publisher()
    instance._is_active = True
    yield instance
    instance.destroy_node()
    rclpy.try_shutdown()


def control(decision: int, sequence: int) -> UtteranceControl:
    message = UtteranceControl()
    message.device_session_id = "capture-a"
    message.utterance_id = 7
    message.onset_sample = 8_000
    message.decision = decision
    message.control_sequence = sequence
    return message


def test_managed_open_uses_exact_sample_index_and_preroll(node: AsrNode) -> None:
    assert node._indexed_pre_roll is not None
    pcm = np.arange(4_000, 12_000, dtype=np.int16)
    node._indexed_pre_roll.push("capture-a", 4_000, 1.0, pcm)

    node._on_input_control(control(UtteranceControl.DECISION_KWS_ONLY, 1))

    assert node._utterance_open is True
    assert node._utterance_id == 7
    assert node._utterance_start_sample == 4_000
    assert node._utterance_next_sample == 12_000
    assert node._prefix_samples == 4_000
    assert node._utterance_chunks[0][0] == 4_000
    event = node._utterance_event_pub.messages[-1]
    assert event.event == UtteranceEvent.EVENT_OPENED


def test_late_control_cannot_downgrade_newer_admission(node: AsrNode) -> None:
    assert node._indexed_pre_roll is not None
    node._indexed_pre_roll.push("capture-a", 4_000, 1.0, np.ones(8_000, dtype=np.int16))
    node._on_input_control(control(UtteranceControl.DECISION_KWS_ONLY, 10))
    node._on_input_control(control(UtteranceControl.DECISION_ADMIT, 11))

    node._on_input_control(control(UtteranceControl.DECISION_REJECT, 10))

    assert node._utterance_open is True
    assert node._utterance_decision == UtteranceControl.DECISION_ADMIT
    assert node._admission[("capture-a", 7)] == UtteranceControl.DECISION_ADMIT


def test_new_reject_discards_only_matching_open_utterance(node: AsrNode) -> None:
    assert node._indexed_pre_roll is not None
    node._indexed_pre_roll.push("capture-a", 4_000, 1.0, np.ones(8_000, dtype=np.int16))
    node._on_input_control(control(UtteranceControl.DECISION_KWS_ONLY, 1))

    node._on_input_control(control(UtteranceControl.DECISION_REJECT, 2))

    assert node._utterance_open is False
    event = node._utterance_event_pub.messages[-1]
    assert event.event == UtteranceEvent.EVENT_DISCARDED


def test_late_old_utterance_cannot_supersede_newer_one(node: AsrNode) -> None:
    assert node._indexed_pre_roll is not None
    node._indexed_pre_roll.push("capture-a", 4_000, 1.0, np.ones(8_000, dtype=np.int16))
    first = control(UtteranceControl.DECISION_KWS_ONLY, 1)
    node._on_input_control(first)
    newer = control(UtteranceControl.DECISION_ADMIT, 2)
    newer.utterance_id = 8
    node._on_input_control(newer)
    stale = control(UtteranceControl.DECISION_REJECT, 3)
    node._on_input_control(stale)

    assert node._utterance_open is True
    assert node._utterance_id == 8
    assert node._admission[("capture-a", 8)] == UtteranceControl.DECISION_ADMIT


def test_control_before_pcm_waits_for_onset_then_opens(node: AsrNode) -> None:
    node._pre_roll = RingBuffer(16_000, max_samples=8_000)
    node._on_input_control(control(UtteranceControl.DECISION_ADMIT, 1))
    assert node._pending_control is not None
    assert node._utterance_open is False

    first = AudioChunk()
    first.device_session_id = "capture-a"
    first.first_sample = 7_000
    first.data = [1] * 500
    node._on_audio(first)
    assert node._utterance_open is False

    second = AudioChunk()
    second.device_session_id = "capture-a"
    second.first_sample = 7_500
    second.data = [2] * 600
    node._on_audio(second)
    assert node._utterance_open is True
    assert node._utterance_start_sample == 7_000
    assert node._utterance_next_sample == 8_100
    assert node._utterance_samples == 1_100


def test_gap_invalidates_queued_final_admission(node: AsrNode) -> None:
    class _Asr:
        def decode(self, _pcm):
            return type("Result", (), {"text": "привет", "confidence": 0.9})()

    node._pre_roll = RingBuffer(16_000, max_samples=8_000)
    node._asr = _Asr()
    node._transcript_pub = _Publisher()
    node._indexed_pre_roll.push("capture-a", 4_000, 1.0, np.ones(8_000, dtype=np.int16))
    node._on_input_control(control(UtteranceControl.DECISION_ADMIT, 1))
    job = _DecodeJob(
        "final",
        np.ones(8_000, dtype=np.int16),
        7,
        1.0,
        4_000,
        8_000,
        250.0,
        "capture-a",
        4_000,
        12_000,
    )
    gap = AudioChunk()
    gap.device_session_id = "capture-a"
    gap.first_sample = 20_000
    gap.data = [0] * 512
    node._on_audio(gap)
    node._run_decode(job)

    assert node._transcript_pub.messages == []
    assert node._utterance_event_pub.messages[-1].event == UtteranceEvent.EVENT_DISCARDED
