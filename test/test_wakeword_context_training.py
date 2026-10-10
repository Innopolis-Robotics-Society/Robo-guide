import numpy as np
import pytest

from scripts import wakeword_context_training as context


def test_trim_keeps_margin_and_rejects_silence():
    audio = np.zeros(16000, dtype=np.int16)
    audio[5000:7000] = 2000
    assert len(context.trim_silence(audio)) == 2000 + 1280
    with pytest.raises(ValueError):
        context.trim_silence(np.zeros(16000, dtype=np.int16))


@pytest.mark.parametrize("position", ["begin", "middle", "end"])
def test_splice_preserves_whole_target_and_returns_placement(position):
    name = np.full(8000, 1200, dtype=np.int16)
    left = np.full(16000, -900, dtype=np.int16)
    right = np.full(32000, 700, dtype=np.int16)
    audio, interval = context.assemble(name, left, right, position)
    assert audio.dtype == np.dtype("<i2")
    assert interval[1] - interval[0] == 0.5
    assert interval[0] == (0 if position == "begin" else 1)
    assert len(audio) / 16000 == interval[1] + (0 if position == "end" else 2)
    assert np.all(audio[round(interval[0] * 16000) : round(interval[1] * 16000)] > 0)
    assert np.abs(audio.astype(float)).max() <= 30000


def test_target_mask_excludes_other_speech_and_allows_embedding_latency():
    mask = context.frame_mask(100, (2, 2.5))
    assert not mask[:24].any()
    assert mask[24:46].all()
    assert not mask[46:].any()


def test_invalid_position_rejected():
    with pytest.raises(ValueError):
        context.assemble(np.ones(20), np.ones(20), np.ones(20), "unknown")
