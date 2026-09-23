"""Расширенное VAD-наблюдение сохраняет capture sample range."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import rclpy

from guide_robot_voice.vad_node import VadNode


class _Vad:
    def process(self, window: np.ndarray) -> float:
        assert window.shape == (512,)
        return 0.91


class _Hysteresis:
    active = False

    def update(self, probability: float) -> object:
        assert probability == 0.91
        self.active = True
        return SimpleNamespace(
            active=True,
            state_duration=0.064,
            segment_ended_too_short=False,
        )


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> None:
        self.messages.append(message)


@pytest.fixture
def node():
    rclpy.init()
    instance = VadNode()
    instance._vad = _Vad()
    instance._hysteresis = _Hysteresis()
    instance._vad_pub = _Publisher()
    instance._observation_pub = _Publisher()
    instance._device_session_id = "capture-42"
    yield instance
    instance.destroy_node()
    rclpy.try_shutdown()


def test_observation_carries_exact_window_range_and_discontinuity(node: VadNode) -> None:
    node._process_window(12.5, 160_000, True, np.ones(512, dtype=np.int16))

    observation = node._observation_pub.messages[-1]
    assert observation.device_session_id == "capture-42"
    assert observation.first_sample == 160_000
    assert observation.sample_count == 512
    assert observation.discontinuity is True
    assert observation.active is True
    assert observation.header.stamp.sec == 12
    assert observation.header.stamp.nanosec == 500_000_000
