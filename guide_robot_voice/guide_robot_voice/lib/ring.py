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

max_samples -- верхняя граница ёмкости для будущего pre-roll в asr_node
(Step 7 design-документа): старые сэмплы вытесняются, а не накапливаются
безгранично. Для audio_frontend (Step 4) не используется (max_samples=None).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

import numpy as np

__all__ = ["RingBuffer"]


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
