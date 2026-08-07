"""Юниты на однополюсный DC-blocker."""

from __future__ import annotations

import numpy as np

from guide_robot_voice.lib.dc_blocker import DcBlocker

SAMPLE_RATE = 48000


def tone_with_offset(frames: int, offset: float, frequency: float = 300.0) -> np.ndarray:
    """Тон с постоянной составляющей, как от смещённого АЦП."""
    index = np.arange(frames)
    signal = np.sin(2 * np.pi * frequency * index / SAMPLE_RATE) * 8000 + offset
    return np.clip(signal, -32768, 32767).astype(np.int16)


def dominant_frequency(pcm: np.ndarray, rate: int) -> float:
    """Частота максимума спектра."""
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    return float(np.fft.rfftfreq(pcm.shape[0], 1 / rate)[np.argmax(spectrum)])


def test_dc_offset_is_removed() -> None:
    """После достаточного числа сэмплов среднее уходит к нулю."""
    blocker = DcBlocker(SAMPLE_RATE, cutoff_hz=40.0)
    source = tone_with_offset(SAMPLE_RATE, offset=5000.0)
    blocks = [blocker.process(source[i : i + 768]) for i in range(0, len(source), 768)]
    out = np.concatenate(blocks)
    # Первые ~10 мс -- переходный процесс фильтра, в среднее не считаем.
    settled = out[SAMPLE_RATE // 10 :]
    assert abs(float(np.mean(settled))) < 200.0


def test_tone_frequency_is_preserved() -> None:
    """Полезный сигнал не искажается по частоте, только офсет уходит."""
    blocker = DcBlocker(SAMPLE_RATE, cutoff_hz=40.0)
    source = tone_with_offset(SAMPLE_RATE, offset=3000.0, frequency=300.0)
    out = blocker.process(source)
    assert abs(dominant_frequency(out, SAMPLE_RATE) - 300.0) < 5.0


def test_state_carries_across_blocks_without_clicks() -> None:
    """Поблочная обработка не даёт скачка на границе кадра.

    Сравнение с разовой обработкой того же сигнала целиком: если состояние
    не переносится, на стыке блоков возникает выброс, которого в разовой
    обработке нет.
    """
    blocker_stream = DcBlocker(SAMPLE_RATE)
    blocker_single = DcBlocker(SAMPLE_RATE)
    source = tone_with_offset(SAMPLE_RATE // 4, offset=2000.0)

    streamed = np.concatenate(
        [blocker_stream.process(source[i : i + 768]) for i in range(0, len(source), 768)]
    )
    single_shot = blocker_single.process(source)

    assert np.allclose(streamed.astype(np.int64), single_shot.astype(np.int64), atol=2)


def test_reset_clears_state() -> None:
    """После reset() фильтр не тащит состояние прошлого потока."""
    blocker = DcBlocker(SAMPLE_RATE)
    blocker.process(tone_with_offset(4800, offset=10000.0))
    blocker.reset()

    fresh = DcBlocker(SAMPLE_RATE)
    source = tone_with_offset(4800, offset=100.0)
    assert np.array_equal(blocker.process(source), fresh.process(source))


def test_naive_matches_scipy_path() -> None:
    """Fallback без scipy даёт тот же результат, что и векторизованный путь."""
    source = tone_with_offset(4800, offset=4000.0)

    scipy_path = DcBlocker(SAMPLE_RATE)
    naive_path = DcBlocker(SAMPLE_RATE)

    scipy_out = scipy_path.process(source)
    x = source.astype(np.float64)
    naive_out = naive_path._filter_naive(x)
    naive_out = np.clip(naive_out, -32768, 32767).astype(np.int16)

    assert np.allclose(scipy_out.astype(np.int64), naive_out.astype(np.int64), atol=1)


def test_silence_stays_silent() -> None:
    """Тишина остаётся тишиной, фильтр не генерирует шум из нуля."""
    blocker = DcBlocker(SAMPLE_RATE)
    out = blocker.process(np.zeros(4800, dtype=np.int16))
    assert np.all(out == 0)


def test_empty_input() -> None:
    """Пустой блок не роняет фильтр и не меняет состояние."""
    blocker = DcBlocker(SAMPLE_RATE)
    assert blocker.process(np.zeros(0, dtype=np.int16)).size == 0
