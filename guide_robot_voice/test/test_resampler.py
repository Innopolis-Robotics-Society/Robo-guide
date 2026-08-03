"""Ресемплинг между частотой модели и частотой устройства."""

from __future__ import annotations

import numpy as np

from guide_robot_voice.lib.resampler import Resampler, resample_int16


def tone(frames: int, rate: int, frequency: float = 440.0) -> np.ndarray:
    """Синус заданной частоты."""
    index = np.arange(frames)
    return (np.sin(2 * np.pi * frequency * index / rate) * 12000).astype(np.int16)


def dominant_frequency(pcm: np.ndarray, rate: int) -> float:
    """Частота максимума спектра."""
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    return float(np.fft.rfftfreq(pcm.shape[0], 1 / rate)[np.argmax(spectrum)])


def test_length_scales_with_rate() -> None:
    """22050 -> 48000: длина растёт пропорционально."""
    source = tone(22050, 22050)
    result = resample_int16(source, 22050, 48000)
    assert abs(result.shape[0] - 48000) < 100


def test_pitch_is_preserved() -> None:
    """Тон остаётся тем же: пересчитывается частота дискретизации, не высота."""
    source = tone(22050, 22050, frequency=440.0)
    result = resample_int16(source, 22050, 48000)
    assert abs(dominant_frequency(result, 48000) - 440.0) < 5.0


def test_passthrough_is_identity() -> None:
    """Совпадающие частоты не трогают данные."""
    source = tone(1000, 48000)
    assert np.array_equal(resample_int16(source, 48000, 48000), source)
    assert Resampler(48000, 48000).passthrough


def test_streaming_matches_single_shot() -> None:
    """Покадровый пересчёт по длине совпадает с разовым.

    Без переноса хвоста между кадрами на каждой границе возникает щелчок,
    и суммарная длина уезжает.
    """
    source = tone(22050, 22050)
    resampler = Resampler(22050, 48000)
    blocks = [resampler.process(source[i : i + 512]) for i in range(0, source.shape[0], 512)]
    streamed = np.concatenate(blocks)
    expected = resample_int16(source, 22050, 48000)
    assert abs(streamed.shape[0] - expected.shape[0]) < expected.shape[0] * 0.02


def test_reset_clears_tail() -> None:
    """После reset() ресемплер не тащит хвост прошлой реплики."""
    resampler = Resampler(22050, 48000)
    resampler.process(tone(512, 22050))
    resampler.reset()
    first = resampler.process(tone(512, 22050))
    fresh = Resampler(22050, 48000).process(tone(512, 22050))
    assert first.shape[0] == fresh.shape[0]


def test_downsample_48k_to_16k() -> None:
    """Путь захвата: 48000 -> 16000, целочисленный делитель 3."""
    source = tone(48000, 48000, frequency=440.0)
    result = resample_int16(source, 48000, 16000)
    assert abs(result.shape[0] - 16000) < 10
    assert abs(dominant_frequency(result, 16000) - 440.0) < 5.0
