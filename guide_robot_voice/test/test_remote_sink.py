from __future__ import annotations

import threading
import time

from guide_robot_msgs.msg import PlaybackState

from guide_robot_voice.lib.remote_sink import RemoteSink


def _sink() -> RemoteSink:
    sink = RemoteSink.__new__(RemoteSink)
    sink._cv = threading.Condition()
    sink._sample_rate = 16_000
    sink._closed = False
    sink._failure = None
    sink._device_session_id = "device-1"
    sink._stream_id = 7
    sink._epoch = 11
    sink._submitted_samples = 16_000
    sink._latest_state = None
    return sink


def _state(state: int, *, generation: int = 11) -> PlaybackState:
    message = PlaybackState()
    message.device_session_id = "device-1"
    message.stream_id = 7
    message.generation = generation
    message.state = state
    message.submitted_samples = 16_000
    message.presented_samples_estimate = 8_000
    message.buffered_samples = 4_000
    return message


def test_remote_sink_reports_only_matching_hardware_state() -> None:
    sink = _sink()
    sink._on_state(_state(PlaybackState.STATE_PLAYING))

    assert sink.is_playing is True
    assert sink.pending_seconds() == 0.25
    assert sink.played_seconds() == 0.5

    sink._on_state(_state(PlaybackState.STATE_PLAYING, generation=10))
    assert sink.is_playing is False
    assert sink.pending_seconds() == 0.0


def test_wait_idle_uses_presented_samples_not_empty_software_queue() -> None:
    sink = _sink()
    sink._on_state(_state(PlaybackState.STATE_DRAINING))
    result: list[bool] = []
    waiter = threading.Thread(target=lambda: result.append(sink.wait_idle(11, timeout=1.0)))
    waiter.start()

    time.sleep(0.01)
    idle = _state(PlaybackState.STATE_IDLE)
    idle.presented_samples_estimate = idle.submitted_samples
    idle.buffered_samples = 0
    sink._on_state(idle)

    waiter.join(timeout=1.0)
    assert result == [True]


def test_wait_idle_rejects_old_generation_immediately() -> None:
    sink = _sink()
    assert sink.wait_idle(10, timeout=1.0) is False


def test_wait_presented_blocks_until_hardware_reaches_clause_checkpoint() -> None:
    sink = _sink()
    sink._on_state(_state(PlaybackState.STATE_PLAYING))
    result: list[bool] = []
    waiter = threading.Thread(target=lambda: result.append(sink.wait_presented(11, timeout=1.0)))
    waiter.start()

    time.sleep(0.01)
    presented = _state(PlaybackState.STATE_DRAINING)
    presented.presented_samples_estimate = 16_000
    sink._on_state(presented)

    waiter.join(timeout=1.0)
    assert result == [True]


def test_wait_presented_rejects_fenced_generation() -> None:
    sink = _sink()
    assert sink.wait_presented(10, timeout=1.0) is False
