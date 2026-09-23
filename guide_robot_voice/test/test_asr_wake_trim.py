"""«Фирая» посреди слитной фразы: asr_node отбрасывает накопленное до обращения."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
from guide_robot_msgs.msg import Wakeword

from guide_robot_voice.asr_node import AsrNode

_RATE = 16000
_PARAMS = {"partial_window_s": 2.0, "wake_trim_margin_s": 1.0, "max_after_wake_s": 8.0}


def _node_with_open_utterance(seconds: float, prefix_s: float = 0.3) -> AsrNode:
    node = AsrNode.__new__(AsrNode)
    node._lock = threading.Lock()
    node._is_active = True
    node._utterance_open = True
    node._utterance_id = 7
    total = int(seconds * _RATE)
    # Чанки по 0.5 с, значения = номер сэмпла: видно, какой кусок остался.
    samples = np.arange(total, dtype=np.int64).astype(np.int16)
    step = _RATE // 2
    node._utterance_chunks = [samples[i : i + step] for i in range(0, total, step)]
    node._utterance_samples = total
    node._prefix_samples = int(prefix_s * _RATE)
    node._utterance_start_sample = 1000
    node._utterance_timestamp = 10.0
    node._wake_cap_ms = 0.0
    node._wake_trims = 0
    node.get_parameter = lambda name: SimpleNamespace(value=_PARAMS[name])
    node.get_logger = lambda: SimpleNamespace(info=lambda _msg: None)
    return node


def test_wakeword_keeps_only_window_plus_margin() -> None:
    node = _node_with_open_utterance(12.0)
    expected_tail = node._utterance_pcm()[-3 * _RATE :].copy()

    node._on_wakeword(Wakeword(keyword="фирая"))

    assert node._utterance_samples == 3 * _RATE
    assert np.array_equal(node._utterance_pcm(), expected_tail)
    assert node._prefix_samples == 0
    assert node._utterance_start_sample == 1000 + 9 * _RATE
    assert node._utterance_timestamp == 10.0 + 9.0
    assert node._wake_trims == 1


def test_wakeword_sets_cap_after_address() -> None:
    node = _node_with_open_utterance(12.0)
    node._on_wakeword(Wakeword(keyword="фирая"))
    assert node._wake_cap_ms == node._utterance_speech_ms() + 8000.0


def test_short_utterance_is_not_trimmed() -> None:
    node = _node_with_open_utterance(2.0)
    before = node._utterance_pcm().copy()
    node._on_wakeword(Wakeword(keyword="фирая"))
    assert np.array_equal(node._utterance_pcm(), before)
    assert node._prefix_samples == int(0.3 * _RATE)
    assert node._wake_trims == 0


def test_no_open_utterance_is_noop() -> None:
    node = _node_with_open_utterance(12.0)
    node._utterance_open = False
    node._on_wakeword(Wakeword(keyword="фирая"))
    assert node._utterance_samples == 12 * _RATE
    assert node._wake_cap_ms == 0.0
