"""Подтверждение остановки PCM должно приходить от аппаратного владельца."""

from __future__ import annotations

import pytest
import rclpy
from guide_robot_msgs.msg import PlaybackState, SpeakingStatus

from guide_robot_voice.voice_session_manager import VoiceSessionManager


@pytest.fixture
def node():
    rclpy.init()
    instance = VoiceSessionManager()
    yield instance
    instance.destroy_node()
    rclpy.try_shutdown()


def test_fenced_playback_requires_empty_fade_and_matching_session(
    node: VoiceSessionManager,
) -> None:
    speaking = SpeakingStatus()
    speaking.stamp = node.get_clock().now().to_msg()
    speaking.speaking = False
    node._latest_speaking = speaking

    playback = PlaybackState()
    playback.stamp = node.get_clock().now().to_msg()
    playback.device_session_id = "capture-a"
    playback.state = PlaybackState.STATE_FENCED
    playback.buffered_samples = 320
    node._latest_playback = playback

    assert node._output_snapshot("capture-a").playback_stopped is False

    playback.buffered_samples = 0
    assert node._output_snapshot("capture-a").playback_stopped is True
    assert node._output_snapshot("capture-b").playback_stopped is False

    playback.stamp.sec -= 2
    assert node._output_snapshot("capture-a").playback_stopped is False
