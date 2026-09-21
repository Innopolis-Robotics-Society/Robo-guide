"""Sample-indexed pre-roll и fencing решений input admission."""

from __future__ import annotations

import numpy as np
import pytest
import rclpy
from guide_robot_msgs.msg import UtteranceControl, UtteranceEvent
from rclpy.parameter import Parameter

from guide_robot_voice.asr_node import AsrNode
from guide_robot_voice.lib.ring import IndexedAudioRing


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
