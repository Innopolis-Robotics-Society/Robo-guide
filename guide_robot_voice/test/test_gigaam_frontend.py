"""Юниты на препроцессинг и CTC-декодирование GigaAM без sherpa-onnx."""

from __future__ import annotations

import numpy as np
import pytest

from guide_robot_voice.lib.gigaam_frontend import ctc_greedy, load_tokens, log_mel

_VOCAB = [" ", "а", "б", "в", "<blk>"]
_BLANK = 4


def _one_hot(ids: list[int], vocab_size: int = 5, p: float = 0.9) -> np.ndarray:
    rest = (1.0 - p) / (vocab_size - 1)
    probs = np.full((len(ids), vocab_size), rest)
    probs[np.arange(len(ids)), ids] = p
    return np.log(probs)


def test_frame_count_matches_center_false_stft() -> None:
    """center=False: 1 + (N - n_fft) // hop кадров, 64 мела."""
    features = log_mel(np.zeros(16000, dtype=np.int16))
    assert features.shape == (64, 1 + (16000 - 320) // 160)
    assert features.dtype == np.float32


def test_silence_is_clamped_not_minus_inf() -> None:
    features = log_mel(np.zeros(3200, dtype=np.int16))
    assert np.isfinite(features).all()
    assert np.allclose(features, np.log(1e-9))


def test_too_short_audio_gives_no_frames() -> None:
    assert log_mel(np.zeros(100, dtype=np.int16)).shape == (64, 0)


def test_tone_energy_lands_in_matching_mel_band() -> None:
    """Тон f -- максимум в полосе, центр которой ближе всего к f по шкале HTK."""
    mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)  # noqa: E731
    centers = np.linspace(0.0, mel(8000.0), 66)[1:-1]
    t = np.arange(16000) / 16000.0
    for freq in (300.0, 1000.0, 3000.0, 6000.0):
        tone = (np.sin(2 * np.pi * freq * t) * 10000).astype(np.int16)
        band = int(np.argmax(log_mel(tone).mean(axis=1)))
        assert abs(band - int(np.argmin(np.abs(centers - mel(freq))))) <= 1, freq


def test_ctc_collapses_repeats_and_drops_blanks() -> None:
    ids = [1, 1, 4, 1, 2, 2, 4, 0, 3, 4]  # а а _ а б б _ ' ' в _
    text, confidence = ctc_greedy(_one_hot(ids), _VOCAB, _BLANK)
    assert text == "ааб в"  # повтор схлопнут, blank разделил две «а»
    assert confidence == pytest.approx(0.9)


def test_ctc_trims_and_squeezes_spaces() -> None:
    text, _ = ctc_greedy(_one_hot([0, 4, 1, 0, 4, 0, 2, 0]), _VOCAB, _BLANK)
    assert text == "а б"


def test_ctc_all_blank_is_empty_with_no_confidence() -> None:
    assert ctc_greedy(_one_hot([4, 4, 4]), _VOCAB, _BLANK) == ("", -1.0)
    assert ctc_greedy(np.zeros((0, 5)), _VOCAB, _BLANK) == ("", -1.0)


def test_load_tokens_sherpa_format(tmp_path) -> None:
    path = tmp_path / "tokens.txt"
    path.write_text("  0\nа 1\nб 2\n<blk> 3\n", encoding="utf-8")
    assert load_tokens(str(path)) == [" ", "а", "б", "<blk>"]
