"""Политика ASR при окончании TTS в legacy и full-duplex режимах."""

from __future__ import annotations

import time

import numpy as np
import pytest
import rclpy
from guide_robot_msgs.msg import SpeakingStatus
from rclpy.parameter import Parameter

from guide_robot_voice.asr_node import AsrNode


@pytest.fixture
def node():
    rclpy.init()
    instance = AsrNode()
    yield instance
    instance.destroy_node()
    rclpy.try_shutdown()


def _status(speaking: bool) -> SpeakingStatus:
    message = SpeakingStatus()
    message.speaking = speaking
    return message


def _open_utterance(node: AsrNode) -> None:
    node._utterance_open = True
    node._utterance_chunks = [np.ones(320, dtype=np.int16)]
    node._utterance_samples = 320


def test_full_duplex_keeps_human_utterance_when_tts_stops(node: AsrNode) -> None:
    node.set_parameters([Parameter("gate_on_tts", value=False)])
    node._latest_speaking = _status(True)
    _open_utterance(node)

    node._on_speaking_status(_status(False))

    assert node._utterance_open is True
    assert node._utterance_samples == 320
    assert node._tts_hold_until == 0.0


def test_legacy_gate_drops_shadow_utterance_and_holds_echo(node: AsrNode) -> None:
    node.set_parameters([Parameter("gate_on_tts", value=True)])
    node._latest_speaking = _status(True)
    _open_utterance(node)
    before = time.monotonic()

    node._on_speaking_status(_status(False))

    assert node._utterance_open is False
    assert node._utterance_samples == 0
    assert node._tts_hold_until > before
