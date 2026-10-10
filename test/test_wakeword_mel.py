import numpy as np
import pytest

from scripts import wakeword_mel as mel


def test_streaming_right_aligned_frontend_matches_offline_windows():
    audio = np.random.default_rng(5).integers(-6000, 6000, 1280 * 6, dtype=np.int16)
    offline = mel.spectrogram(np.concatenate([np.zeros(32000, np.int16), audio]))
    buffer = np.zeros(mel.WINDOW + (mel.CONTEXT - 1) * mel.HOP, np.float32)
    for first in range(0, len(audio), 1280):
        buffer = np.concatenate(
            [buffer[1280:], audio[first : first + 1280].astype(np.float32) / 32768]
        )
        frames = np.lib.stride_tricks.sliding_window_view(buffer, mel.WINDOW)[:: mel.HOP]
        actual = mel._transform(frames)
        expected = mel.feature_window(offline, 200 + (first + 1280) // mel.HOP)
        assert actual.shape == (160, 80)
        np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)


def test_silence_is_finite_and_filterbank_nonnegative():
    values = mel.spectrogram(np.zeros(1280, np.int16))
    assert values.shape == (8, 80) and np.isfinite(values).all()
    assert (mel.FILTERS >= 0).all()
    assert (mel.FILTERS.sum(axis=1) > 0).all()


def test_mel_head_input_output_and_gradient():
    torch = pytest.importorskip("torch")
    head = mel.build_model(np.zeros(80, np.float32), np.ones(80, np.float32))
    values = torch.randn(2, 160, 80, requires_grad=True)
    output = head(values)
    assert output.shape == (2, 1)
    output.sum().backward()
    assert torch.isfinite(values.grad).all()
