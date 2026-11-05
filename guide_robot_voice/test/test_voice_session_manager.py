"""Подтверждение остановки PCM должно приходить от аппаратного владельца."""

from __future__ import annotations

import pytest
import rclpy
from guide_robot_msgs.msg import PlaybackState, SpeakingStatus

from guide_robot_voice.lib.session_policy import OutputSnapshot, VadWindow
from guide_robot_voice.lib.wakeword_session_policy import WakewordSessionPolicy
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


class _Recorder:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, msg) -> None:
        self.messages.append(msg)


def test_state_published_on_change_and_by_heartbeat(node: VoiceSessionManager) -> None:
    policy = WakewordSessionPolicy()
    policy.activate()
    node._policy = policy
    node._active = True
    node._state_pub = recorder = _Recorder()

    node._publish_state()
    node._publish_state()
    node._publish_state()
    assert len(recorder.messages) == 1

    node._publish_state(force=True)
    assert len(recorder.messages) == 2

    # Первое VAD-окно переводит INITIALIZING -> ARMED.
    idle = OutputSnapshot(speaking_fresh=True, playback_stopped=True)
    policy.observe(VadWindow("a", 0, 512, 0.0, 0.0, False, False), idle)
    node._publish_state()
    assert len(recorder.messages) == 3
    assert recorder.messages[-1].transition_sequence > recorder.messages[0].transition_sequence
