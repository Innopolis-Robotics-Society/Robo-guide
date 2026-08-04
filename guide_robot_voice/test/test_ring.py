"""Юниты на FIFO-буфер с привязкой времени к кускам переменной длины."""

from __future__ import annotations

import numpy as np

from guide_robot_voice.lib.ring import RingBuffer

SAMPLE_RATE = 16000


def block(value: int, n: int) -> np.ndarray:
    """Кусок PCM, целиком заполненный меткой value."""
    return np.full(n, value, dtype=np.int16)


def test_pop_returns_none_until_enough_samples() -> None:
    """Не хватает данных -- вернуть None, а не подложить тишину."""
    ring = RingBuffer(SAMPLE_RATE)
    ring.push(0.0, block(1, 100))
    assert ring.pop_exact(256) is None
    assert len(ring) == 100


def test_pop_exact_returns_requested_length() -> None:
    """Как только данных достаточно -- отдаётся ровно запрошенное число."""
    ring = RingBuffer(SAMPLE_RATE)
    ring.push(0.0, block(1, 300))
    result = ring.pop_exact(256)
    assert result is not None
    _, samples = result
    assert samples.shape[0] == 256
    assert len(ring) == 44


def test_frame_spanning_two_pushes_has_correct_data_and_timestamp() -> None:
    """Кадр, собранный из хвостов двух push(), содержит верные данные и штамп.

    Штамп кадра -- время ПЕРВОГО сэмпла именно этого кадра, а не время
    последнего callback'а, который его дополнил.
    """
    ring = RingBuffer(SAMPLE_RATE)
    ring.push(10.0, block(1, 100))
    ring.push(10.0 + 100 / SAMPLE_RATE, block(2, 200))

    timestamp, samples = ring.pop_exact(150)
    assert timestamp == 10.0
    assert np.all(samples[:100] == 1)
    assert np.all(samples[100:150] == 2)

    # Второй кадр начинается на 50-м сэмпле второго push -- штамп сдвинут.
    timestamp2, samples2 = ring.pop_exact(150)
    expected_ts2 = 10.0 + 100 / SAMPLE_RATE + 50 / SAMPLE_RATE
    assert abs(timestamp2 - expected_ts2) < 1e-9
    assert np.all(samples2 == 2)


def test_max_samples_evicts_oldest() -> None:
    """Переполнение ёмкости вытесняет старые сэмплы, а не растит буфер."""
    ring = RingBuffer(SAMPLE_RATE, max_samples=200)
    ring.push(0.0, block(1, 150))
    ring.push(150 / SAMPLE_RATE, block(2, 150))
    assert len(ring) == 200

    result = ring.pop_exact(200)
    assert result is not None
    timestamp, samples = result
    # Первые 100 сэмплов первого push вытеснены; кадр начинается
    # с сэмпла 50 первого push.
    assert abs(timestamp - 100 / SAMPLE_RATE) < 1e-9
    assert np.all(samples[:50] == 1)
    assert np.all(samples[50:] == 2)


def test_empty_push_is_noop() -> None:
    """push() с пустым массивом не создаёт сегмент и не портит штампы."""
    ring = RingBuffer(SAMPLE_RATE)
    ring.push(0.0, np.zeros(0, dtype=np.int16))
    ring.push(0.0, block(1, 256))
    assert len(ring) == 256
    result = ring.pop_exact(256)
    assert result is not None
    timestamp, _ = result
    assert timestamp == 0.0


def test_snapshot_returns_none_when_empty() -> None:
    """Пустой буфер -- snapshot() возвращает None, а не пустой массив."""
    ring = RingBuffer(SAMPLE_RATE)
    assert ring.snapshot() is None


def test_snapshot_does_not_consume() -> None:
    """snapshot() -- для pre-roll: читает, не извлекая, буфер продолжает копить."""
    ring = RingBuffer(SAMPLE_RATE, max_samples=300)
    ring.push(0.0, block(1, 100))
    ring.push(100 / SAMPLE_RATE, block(2, 100))

    result = ring.snapshot()
    assert result is not None
    timestamp, samples = result
    assert timestamp == 0.0
    assert samples.shape[0] == 200
    assert np.all(samples[:100] == 1)
    assert np.all(samples[100:] == 2)

    # Буфер не пострадал -- следующий push() продолжает копить как ни в чём не бывало.
    assert len(ring) == 200
    ring.push(200 / SAMPLE_RATE, block(3, 50))
    assert len(ring) == 250
