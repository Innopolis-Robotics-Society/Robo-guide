"""Новый ORT-бэкенд сохраняет CTC-тайминги для acoustic wake admission."""

from types import SimpleNamespace

import numpy as np
import pytest

from guide_robot_voice.lib.asr_model import OrtGigaAmCtc
from guide_robot_voice.lib.wakeword_command import command_after_wake


@pytest.mark.parametrize("text", ["ферая", "фера я", "сырая"])
def test_ort_wake_fragment_keeps_command_window(text):
    vocab = [*dict.fromkeys(text), "<blk>"]
    blank = len(vocab) - 1
    ids = [blank] * 10
    for char in text:
        ids.extend([vocab.index(char), blank])
    logits = np.full((1, len(ids), len(vocab)), -20.0, dtype=np.float32)
    logits[0, np.arange(len(ids)), ids] = 0.0
    asr = OrtGigaAmCtc("unused.onnx", "unused.txt")
    asr._session = SimpleNamespace(run=lambda _outputs, _inputs: [logits])
    asr._vocab = vocab
    asr._blank = blank

    result = asr.decode(np.zeros(16000, dtype=np.int16))

    assert result.text == text and "".join(result.tokens) == text
    assert result.timestamps[0] == pytest.approx(0.4)
    command, aligned = command_after_wake(
        result.text, result.tokens, result.timestamps, 1.2, ["фирая"]
    )
    assert command == "" and aligned
