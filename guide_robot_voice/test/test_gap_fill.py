"""Юниты на заполнение коротких пропусков /audio/mic тишиной."""

from __future__ import annotations

import numpy as np

from guide_robot_voice.lib.gap_fill import fill_small_gap


def _block(n: int, value: int = 7) -> np.ndarray:
    return np.full(n, value, dtype=np.int16)


def test_small_forward_gap_is_filled_with_silence() -> None:
    first, samples, filled = fill_small_gap(1000, 1256, _block(256), 1600)
    assert (first, filled) == (1000, 256)
    assert samples.shape[0] == 512
    assert not samples[:256].any()
    assert (samples[256:] == 7).all()


def test_contiguous_block_is_untouched() -> None:
    block = _block(256)
    first, samples, filled = fill_small_gap(1000, 1000, block, 1600)
    assert (first, filled) == (1000, 0)
    assert samples is block


def test_long_gap_stays_a_discontinuity() -> None:
    first, samples, filled = fill_small_gap(1000, 1000 + 1601, _block(256), 1600)
    assert (first, filled) == (2601, 0)
    assert samples.shape[0] == 256


def test_backward_jump_and_unknown_history_are_not_filled() -> None:
    assert fill_small_gap(1000, 900, _block(256), 1600)[2] == 0
    assert fill_small_gap(None, 900, _block(256), 1600)[2] == 0


def test_disabled_when_limit_is_zero() -> None:
    assert fill_small_gap(1000, 1100, _block(256), 0)[2] == 0
