"""Анализ петлевого измерения на синтетике, без железа."""

from __future__ import annotations

import numpy as np
import pytest

from guide_robot_voice.audio.loopback import (
    Recording,
    analyse_stop,
    envelope,
    estimate_offset,
    noise_burst,
)

RATE = 48000
BURST_AT = int(RATE * 0.3)
BURST_LEN = int(RATE * 0.03)


def tone(frames: int, amplitude: int = 12000) -> np.ndarray:
    """Синус 1 кГц заданной длины."""
    index = np.arange(frames)
    return (np.sin(2 * np.pi * 1000 * index / RATE) * amplitude).astype(np.int16)


def build(offset: int, tone_frames: int, tail: int, transient: bool = False) -> Recording:
    """Синтетическая пара дорожек с калибровочным всплеском и тоном.

    Выход обрывается ровно на tone_frames -- это момент abort().
    Вход продолжает писать ещё tail кадров: именно так ведёт себя
    независимый поток захвата.
    """
    tone_at = BURST_AT + BURST_LEN + int(RATE * 0.5)
    out_len = tone_at + tone_frames
    out = np.zeros(out_len, dtype=np.int16)
    out[BURST_AT : BURST_AT + BURST_LEN] = noise_burst(BURST_LEN)
    out[tone_at:] = tone(tone_frames)

    inp = np.zeros(out_len + offset + tail, dtype=np.int16)
    inp[BURST_AT + offset : BURST_AT + offset + BURST_LEN] = noise_burst(BURST_LEN, amplitude=6000)
    inp[tone_at + offset : tone_at + offset + tone_frames + tail] = tone(
        tone_frames + tail, amplitude=6000
    )
    if transient:
        # Хлопок при открытии потока ALSA, громче полезного сигнала.
        inp[:256] = 30000
    return Recording(out=out, inp=inp)


def test_envelope_shape() -> None:
    """RMS-огибающая укорачивает сигнал в block раз."""
    assert envelope(np.zeros(640, dtype=np.int16), block=64).shape == (10,)


def test_offset_survives_startup_transient() -> None:
    """Хлопок при открытии потока не сбивает калибровку."""
    offset = 4800
    recording = build(offset, tone_frames=RATE, tail=0, transient=True)
    assert abs(estimate_offset(recording, RATE) - offset) <= 128


def test_recovers_known_stop_point() -> None:
    """Известный хвост после отмены восстанавливается.

    tail -- это то, что доиграло из буфера устройства уже после abort().
    Наблюдаемо только потому, что захват идёт отдельным потоком.
    """
    offset = 4800
    tail = int(RATE * 0.08)  # 80 мс доиграло
    tone_frames = RATE
    recording = build(offset, tone_frames=tone_frames, tail=tail)
    mark_out = recording.out.shape[0]

    result = analyse_stop(recording, mark_out, RATE)
    assert result.valid, result.warnings
    assert abs(result.t_stop_ms - 80.0) < 5.0


def test_instant_stop_reads_near_zero() -> None:
    """Если abort() выбросил буфер, t_stop около нуля."""
    offset = 4800
    recording = build(offset, tone_frames=RATE, tail=0)
    result = analyse_stop(recording, recording.out.shape[0], RATE)
    assert result.valid, result.warnings
    assert abs(result.t_stop_ms) < 5.0


def test_stop_before_cancel_is_flagged() -> None:
    """Обрыв раньше отмены помечается недостоверным, а не печатается как вывод."""
    recording = build(4800, tone_frames=RATE, tail=0)
    result = analyse_stop(recording, recording.out.shape[0] + RATE // 2, RATE)
    assert not result.valid
    assert any("раньше отмены" in w for w in result.warnings)


def test_missing_burst_in_output_is_explained() -> None:
    """Отсутствие калибровочного всплеска называет окно, куда он должен попасть."""
    silence = np.zeros(RATE, dtype=np.int16)
    recording = Recording(out=silence, inp=silence.copy())
    with pytest.raises(ValueError, match="окно"):
        analyse_stop(recording, 0, RATE)


def test_silent_input_is_explained() -> None:
    """Молчащий вход отличается от отсутствующего выхода."""
    recording = build(4800, tone_frames=RATE, tail=0)
    silent = Recording(out=recording.out, inp=np.zeros_like(recording.inp))
    with pytest.raises(ValueError, match="тишина"):
        analyse_stop(silent, 0, RATE)
