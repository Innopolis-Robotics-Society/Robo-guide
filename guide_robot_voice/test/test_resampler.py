"""Ресемплинг между частотой модели и частотой устройства."""

from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly

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
    """Покадровый пересчёт по длине совпадает с разовым после flush().

    Без переноса состояния между кадрами на каждой границе возникает
    щелчок, и суммарная длина уезжает. С `soxr` часть сэмплов на конце
    остаётся в буфере фильтра до явного `flush()` -- это нормальное
    поведение стейтфул-ресемплера (та же причина, по которой tts_node
    обязан звать `flush()` на чистом конце реплики), не дрейф.
    """
    source = tone(22050, 22050)
    resampler = Resampler(22050, 48000)
    blocks = [resampler.process(source[i : i + 512]) for i in range(0, source.shape[0], 512)]
    blocks.append(resampler.flush())
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


def _snr_db(streamed: np.ndarray, reference: np.ndarray) -> float:
    """SNR потокового результата против разового resample_poly всего сигнала."""
    signal_power = np.sum(reference.astype(np.float64) ** 2)
    noise_power = np.sum((streamed.astype(np.float64) - reference.astype(np.float64)) ** 2)
    if noise_power <= 0:
        return float("inf")
    return float(10 * np.log10(signal_power / noise_power))


def _reference_48k_to_16k(source: np.ndarray) -> np.ndarray:
    result = resample_poly(source.astype(np.float32), 1, 3)
    return np.clip(result, -32768, 32767).astype(np.int16)


def _streamed_48k_to_16k(source: np.ndarray, block_samples: int) -> np.ndarray:
    resampler = Resampler(48000, 16000)
    blocks = [
        resampler.process(source[i : i + block_samples])
        for i in range(0, source.shape[0], block_samples)
    ]
    blocks.append(resampler.flush())
    return np.concatenate(blocks)


def test_block_resampling_matches_single_shot_snr_and_length() -> None:
    """B1 приёмочный тест: 440Hz, 60с, 48000->16000 поблочно (16мс/кадр).

    0 сэмплов дрейфа по длине и SNR >= 60dB против разового resample_poly
    -- пороги CLAUDE_CODE_TASK_stage3_voice_consistency.md. Раньше `skip`
    считался `round(overlap*target/source)` вместо точной формулы
    `resample_poly` -- расхождение копилось без ограничения (~+62
    сэмпла/сек на этой паре частот).
    """
    duration_s = 60
    source = tone(48000 * duration_s, 48000, frequency=440.0)
    reference = _reference_48k_to_16k(source)
    streamed = _streamed_48k_to_16k(source, block_samples=768)

    assert streamed.shape[0] == reference.shape[0]
    assert _snr_db(streamed, reference) >= 60.0


def test_block_resampling_snr_is_independent_of_block_size() -> None:
    """Бухгалтерия по абсолютной позиции -- SNR не должен зависеть от нарезки.

    10мс/20мс/32мс блоки (480/960/1536 сэмплов на 48kHz) на одном и том же
    сигнале должны давать практически идентичный SNR -- если бы `skip`
    всё ещё считался по кадру, разная нарезка давала бы разный дрейф.
    """
    source = tone(48000 * 5, 48000, frequency=440.0)
    reference = _reference_48k_to_16k(source)

    snrs = [
        _snr_db(_streamed_48k_to_16k(source, block_samples=n), reference)
        for n in (480, 960, 1536)
    ]
    assert all(snr >= 60.0 for snr in snrs)
    assert max(snrs) - min(snrs) < 0.5
