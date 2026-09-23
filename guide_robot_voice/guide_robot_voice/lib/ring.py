"""FIFO-буфер int16 моно с привязкой времени к каждому куску.

Зачем это отдельный модуль, а не просто конкатенация массивов.

После ресемплинга длина блока от вызова к вызову плавает на 1-2 сэмпла
(polyphase-фильтр с переносимым хвостом даёт точную длину только в сумме,
не на каждом отдельном вызове -- см. lib/resampler.py). Downstream (VAD --
512 сэмплов, openWakeWord -- 1280) требуют кадры ровного размера. Значит,
между ресемплером и публикацией обязан стоять буфер, который накапливает
куски переменной длины и отдаёт кадры фиксированной.

Второе и более тонкое требование -- штамп времени. Кадр, отданный
pop_exact(), может быть собран из хвостов ДВУХ разных callback'ов захвата
с разными моментами прихода. Штамп времени этого кадра обязан быть
временем его СОБСТВЕННОГО первого сэмпла, а не временем последнего
callback'а -- иначе весь бюджет barge-in (design §4) считается неверно.
Поэтому push() принимает временную метку куска, а pop_exact() возвращает
корректно смещённую метку начала кадра, даже если кадр составной.

max_samples -- верхняя граница ёмкости для pre-roll в asr_node: старые
сэмплы вытесняются, а не накапливаются безгранично. Для audio_frontend
и vad_node не используется (max_samples=None, чистый FIFO). Для pre-roll
дополнительно нужен snapshot() -- неразрушающее чтение: буфер обязан
продолжать копить последние pre_roll_ms и после срабатывания VAD, для
следующего высказывания.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

import numpy as np

__all__ = ["IndexedAudioRing", "IndexedSnapshot", "RingBuffer"]


@dataclass
class _Segment:
    timestamp: float
    samples: np.ndarray


class RingBuffer:
    """Накопитель кусков int16 моно с извлечением кадров фиксированной длины."""

    def __init__(self, sample_rate: int, max_samples: int | None = None) -> None:
        """Создать буфер. sample_rate нужен для арифметики штампов времени."""
        self._sample_rate = sample_rate
        self._max_samples = max_samples
        self._segments: collections.deque[_Segment] = collections.deque()
        self._length = 0

    def __len__(self) -> int:
        """Сколько сэмплов сейчас в буфере."""
        return self._length

    def push(self, timestamp: float, samples: np.ndarray) -> None:
        """Добавить кусок сэмплов со временем его первого сэмпла."""
        if samples.size == 0:
            return
        self._segments.append(_Segment(timestamp, samples))
        self._length += int(samples.shape[0])
        if self._max_samples is not None:
            self._evict_excess()

    def _evict_excess(self) -> None:
        assert self._max_samples is not None
        while self._length > self._max_samples and self._segments:
            head = self._segments[0]
            head_len = int(head.samples.shape[0])
            overflow = self._length - self._max_samples
            if overflow >= head_len:
                self._segments.popleft()
                self._length -= head_len
            else:
                self._segments[0] = _Segment(
                    timestamp=head.timestamp + overflow / self._sample_rate,
                    samples=head.samples[overflow:],
                )
                self._length -= overflow

    def pop_exact(self, n: int) -> tuple[float, np.ndarray] | None:
        """Извлечь ровно n сэмплов и штамп времени первого из них.

        None, если сэмплов ещё недостаточно -- вызывающий обязан подождать
        следующего push(), а не подкладывать тишину вместо недостающих
        данных (та же логика, что и в EpochFencedSink).
        """
        if self._length < n:
            return None
        start_timestamp = self._segments[0].timestamp
        out = np.empty(n, dtype=np.int16)
        filled = 0
        while filled < n:
            head = self._segments[0]
            head_len = int(head.samples.shape[0])
            take = min(head_len, n - filled)
            out[filled : filled + take] = head.samples[:take]
            filled += take
            if take == head_len:
                self._segments.popleft()
            else:
                self._segments[0] = _Segment(
                    timestamp=head.timestamp + take / self._sample_rate,
                    samples=head.samples[take:],
                )
        self._length -= n
        return start_timestamp, out

    def snapshot(self) -> tuple[float, np.ndarray] | None:
        """Отдать всё содержимое буфера БЕЗ извлечения -- для pre-roll.

        В отличие от pop_exact(), не потребляет данные: pre-roll читается
        в момент срабатывания VAD, а буфер обязан продолжать копить
        последние pre_roll_ms и после этого момента, для следующего
        высказывания. None, если буфер пуст.
        """
        if not self._segments:
            return None
        start_timestamp = self._segments[0].timestamp
        return start_timestamp, np.concatenate([seg.samples for seg in self._segments])


@dataclass
class _IndexedSegment:
    first_sample: int
    timestamp: float
    samples: np.ndarray


@dataclass(frozen=True)
class IndexedSnapshot:
    """Непрерывный срез indexed pre-roll и его точные границы."""

    device_session_id: str
    first_sample: int
    next_sample: int
    timestamp: float
    samples: np.ndarray
    underflow: bool


class IndexedAudioRing:
    """Ограниченный pre-roll с адресацией по capture sample index.

    В отличие от ``RingBuffer``, этот буфер не скрывает разрывы между push:
    новая device-сессия, rewind или gap атомарно начинают новую историю.
    Это не позволяет ASR случайно склеить звук до и после USB reconnect.
    """

    def __init__(self, sample_rate: int, max_samples: int) -> None:
        """Создать буфер заданной частоты и ограниченной ёмкости."""
        if sample_rate <= 0 or max_samples <= 0:
            raise ValueError("sample_rate и max_samples должны быть положительными")
        self._sample_rate = sample_rate
        self._max_samples = max_samples
        self._segments: collections.deque[_IndexedSegment] = collections.deque()
        self._length = 0
        self._device_session_id = ""
        self._next_sample: int | None = None

    def __len__(self) -> int:
        """Вернуть число доступных сэмплов."""
        return self._length

    @property
    def device_session_id(self) -> str:
        """Вернуть сессию текущей непрерывной истории."""
        return self._device_session_id

    @property
    def next_sample(self) -> int | None:
        """Вернуть индекс сразу после последнего доступного сэмпла."""
        return self._next_sample

    @property
    def first_sample(self) -> int | None:
        """Вернуть индекс старейшего доступного сэмпла."""
        return self._segments[0].first_sample if self._segments else None

    def clear(self) -> None:
        """Удалить историю и идентификатор сессии."""
        self._segments.clear()
        self._length = 0
        self._device_session_id = ""
        self._next_sample = None

    def push(
        self,
        device_session_id: str,
        first_sample: int,
        timestamp: float,
        samples: np.ndarray,
    ) -> bool:
        """Добавить блок; вернуть True, если перед ним обнаружен разрыв."""
        if samples.size == 0:
            return False
        discontinuity = (
            not self._device_session_id
            or device_session_id != self._device_session_id
            or self._next_sample is None
            or first_sample != self._next_sample
        )
        if discontinuity:
            self._segments.clear()
            self._length = 0
            self._device_session_id = device_session_id

        # Хранить собственную копию: callback может повторно использовать
        # исходный ndarray до того, как ASR снимет pre-roll.
        owned = np.array(samples, dtype=np.int16, copy=True)
        self._segments.append(_IndexedSegment(first_sample, timestamp, owned))
        count = int(owned.shape[0])
        self._length += count
        self._next_sample = first_sample + count
        self._evict_excess()
        return discontinuity

    def _evict_excess(self) -> None:
        while self._length > self._max_samples and self._segments:
            head = self._segments[0]
            head_len = int(head.samples.shape[0])
            overflow = self._length - self._max_samples
            if overflow >= head_len:
                self._segments.popleft()
                self._length -= head_len
                continue
            self._segments[0] = _IndexedSegment(
                first_sample=head.first_sample + overflow,
                timestamp=head.timestamp + overflow / self._sample_rate,
                samples=head.samples[overflow:],
            )
            self._length -= overflow

    def snapshot_from(
        self, device_session_id: str, requested_first_sample: int
    ) -> IndexedSnapshot | None:
        """Вернуть ``[requested, current_end)`` без потребления буфера.

        Если запрошенное начало уже вытеснено, срез начинается с самого
        старого доступного сэмпла и ``underflow`` явно становится True.
        """
        if not self._segments or device_session_id != self._device_session_id:
            return None
        available_first = self._segments[0].first_sample
        available_next = self._next_sample
        assert available_next is not None
        if requested_first_sample > available_next:
            return None
        actual_first = max(requested_first_sample, available_first)
        actual_first = min(actual_first, available_next)
        underflow = requested_first_sample < available_first

        parts: list[np.ndarray] = []
        timestamp = (
            self._segments[-1].timestamp + len(self._segments[-1].samples) / self._sample_rate
        )
        for segment in self._segments:
            segment_next = segment.first_sample + int(segment.samples.shape[0])
            if segment_next <= actual_first:
                continue
            offset = max(0, actual_first - segment.first_sample)
            if not parts:
                timestamp = segment.timestamp + offset / self._sample_rate
            parts.append(segment.samples[offset:])

        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        return IndexedSnapshot(
            device_session_id=device_session_id,
            first_sample=actual_first,
            next_sample=available_next,
            timestamp=timestamp,
            samples=samples,
            underflow=underflow,
        )

    def snapshot(self) -> IndexedSnapshot | None:
        """Вернуть всю доступную непрерывную историю."""
        first = self.first_sample
        if first is None:
            return None
        return self.snapshot_from(self._device_session_id, first)
