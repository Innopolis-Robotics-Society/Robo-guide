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


class _QuietVad:
    def __init__(self) -> None:
        self.resets = 0

    def process(self, window: np.ndarray) -> float:
        return 0.0

    def reset(self) -> None:
        self.resets += 1


class _QuietHysteresis:
    def __init__(self, active_windows: set[int]) -> None:
        self.calls = 0
        self.active_windows = active_windows

    def update(self, probability: float) -> object:
        self.calls += 1
        return SimpleNamespace(
            active=self.calls in self.active_windows,
            state_duration=0.032,
            segment_ended_too_short=False,
        )


def test_long_silence_resets_only_the_model_every_idle_period(node: VadNode) -> None:
    node._vad = _QuietVad()
    node._hysteresis = _QuietHysteresis(active_windows={300})
    node._idle_reset_s = 8.0
    for index in range(560):
        node._process_window(index * 0.032, index * 512, False, np.zeros(512, dtype=np.int16))
    # ~250 окон по 32 мс = 8 с тишины -> сброс; речь на 300-м окне обнуляет отсчёт.
    assert node._vad.resets == node._idle_resets_total == 2
    assert node._hysteresis.calls == 560


def test_idle_reset_disabled_by_default(node: VadNode) -> None:
    node._vad = _QuietVad()
    node._hysteresis = _QuietHysteresis(active_windows=set())
    for index in range(1000):
        node._process_window(index * 0.032, index * 512, False, np.zeros(512, dtype=np.int16))
    assert node._vad.resets == 0
