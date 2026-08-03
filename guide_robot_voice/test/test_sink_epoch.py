"""Инвариант fencing: устаревшие сэмплы не попадают в устройство."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from guide_robot_voice.lib.sink import EpochFencedSink, MemoryEmitter, SinkFailureError

SAMPLE_RATE = 16000
BLOCK = 320


def tagged_chunk(epoch: int, frames: int = BLOCK) -> np.ndarray:
    """PCM-кадр, целиком заполненный меткой epoch (epoch >= 1)."""
    return np.full(frames, epoch, dtype=np.int16)


def emitted_marks(emitter: MemoryEmitter) -> set[int]:
    """Все ненулевые значения, дошедшие до устройства."""
    marks: set[int] = set()
    for block in emitter.writes:
        marks.update(int(v) for v in np.unique(block) if v != 0)
    return marks


def test_stale_samples_are_never_emitted() -> None:
    """После bump() ни один сэмпл со старым epoch не выдан в устройство."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.002)
    sink = EpochFencedSink(emitter, SAMPLE_RATE, max_queue_ms=10_000)
    sink.start()
    try:
        epoch = sink.epoch + 1
        sink.bump("prime")
        for _ in range(200):
            sink.submit(epoch, tagged_chunk(epoch))
        time.sleep(0.02)

        new_epoch = sink.bump("barge_in")
        before = len(emitter.writes)
        time.sleep(0.05)

        after_abort = emitter.writes[before:]
        assert all(int(v) == 0 for block in after_abort for v in np.unique(block))
        assert new_epoch == epoch + 1
    finally:
        sink.close()


def test_new_epoch_plays_after_cancel() -> None:
    """Отмена не выводит сток из строя: следующая реплика звучит."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.002)
    sink = EpochFencedSink(emitter, SAMPLE_RATE, max_queue_ms=10_000)
    sink.start()
    try:
        first = sink.bump("prime")
        for _ in range(50):
            sink.submit(first, tagged_chunk(first))
        time.sleep(0.02)
        second = sink.bump("barge_in")
        for _ in range(20):
            sink.submit(second, tagged_chunk(second))
        time.sleep(0.08)
        assert second in emitted_marks(emitter)
    finally:
        sink.close()


def test_submit_rejects_stale_epoch() -> None:
    """submit() со старым epoch возвращает False -- сигнал прекратить синтез."""
    emitter = MemoryEmitter(block=BLOCK)
    sink = EpochFencedSink(emitter, SAMPLE_RATE)
    sink.start()
    try:
        stale = sink.epoch
        sink.bump("estop")
        assert sink.submit(stale, tagged_chunk(1)) is False
    finally:
        sink.close()


def test_partial_chunk_is_resumed() -> None:
    """Чанк крупнее периода колбэка доигрывается по частям без потерь."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.001)
    sink = EpochFencedSink(emitter, SAMPLE_RATE, max_queue_ms=10_000)
    sink.start()
    try:
        epoch = sink.bump("prime")
        sink.submit(epoch, tagged_chunk(epoch, frames=BLOCK * 5))
        assert sink.wait_idle(epoch, timeout=2.0)
        total = sum(int(np.count_nonzero(b == epoch)) for b in emitter.writes)
        assert total == BLOCK * 5
    finally:
        sink.close()


def test_wait_idle_returns_false_when_preempted() -> None:
    """wait_idle() честно сообщает об отмене, а не таймаутит."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.005)
    sink = EpochFencedSink(emitter, SAMPLE_RATE, max_queue_ms=10_000)
    sink.start()
    try:
        epoch = sink.bump("prime")
        for _ in range(100):
            sink.submit(epoch, tagged_chunk(epoch))
        result: list[bool] = []
        waiter = threading.Thread(target=lambda: result.append(sink.wait_idle(epoch, 5.0)))
        waiter.start()
        time.sleep(0.02)
        sink.bump("barge_in")
        waiter.join(timeout=2.0)
        assert result == [False]
    finally:
        sink.close()


def test_software_stop_latency_is_bounded() -> None:
    """Софтовая часть t_stop -- единицы миллисекунд, узкое место в устройстве."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.002)
    sink = EpochFencedSink(emitter, SAMPLE_RATE, max_queue_ms=10_000)
    sink.start()
    try:
        epoch = sink.bump("prime")
        for _ in range(300):
            sink.submit(epoch, tagged_chunk(epoch))
        time.sleep(0.02)
        sink.bump("barge_in")
        assert sink.metrics.t_stop_ms < 10.0
        assert sink.metrics.dropped_chunks > 0
    finally:
        sink.close()


def test_underflow_is_counted() -> None:
    """Нехватка данных считается, а не маскируется тишиной молча."""
    emitter = MemoryEmitter(block=BLOCK, interval=0.001)
    sink = EpochFencedSink(emitter, SAMPLE_RATE)
    sink.start()
    try:
        time.sleep(0.03)
        assert sink.underflows > 0
    finally:
        sink.close()


class BrokenEmitter(MemoryEmitter):
    """Эмиттер, у которого не открывается устройство."""

    def open(self, pull: object) -> None:
        """Отказать сразу, как отказало бы реальное устройство."""
        raise ValueError("No output device matching 'USB Headset'")


def test_open_failure_reaches_caller() -> None:
    """Отказ открытия прилетает вызывающему, а не тонет в потоке.

    Регрессия на реальный случай: устройство открывалось лениво внутри
    писательского потока, исключение терялось, и measure_t_stop печатал
    0.19 мс при нулевом звуке.
    """
    sink = EpochFencedSink(BrokenEmitter(), SAMPLE_RATE)
    with pytest.raises(ValueError, match="No output device"):
        sink.start()


def test_callback_failure_is_visible() -> None:
    """Отказ в колбэке виден через raise_if_failed()."""

    class FailingEmitter(MemoryEmitter):
        def open(self, pull: object) -> None:
            super().open(pull)
            self.failure = RuntimeError("устройство отвалилось")

    emitter = FailingEmitter(block=BLOCK)
    sink = EpochFencedSink(emitter, SAMPLE_RATE)
    sink.start()
    try:
        assert sink.failure is not None
        assert sink.submit(sink.epoch, tagged_chunk(1)) is False
        with pytest.raises(SinkFailureError):
            sink.raise_if_failed()
    finally:
        sink.close()
