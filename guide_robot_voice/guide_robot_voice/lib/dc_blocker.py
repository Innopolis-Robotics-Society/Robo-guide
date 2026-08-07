"""Однополюсный HPF (DC blocker) для подавления постоянной составляющей.

Зачем отдельно от gain. Постоянная составляющая на входе АЦП искажает
измерение уровня (level_dbfs) и способна свести VAD с ума на границе
порога, если офсет плавает от температуры или питания USB. Классический
"DC blocker" (Julius O. Smith): y[n] = x[n] - x[n-1] + R*y[n-1], R чуть
меньше 1. Состояние (последний вход и последний выход) переносится между
кадрами -- иначе на каждой границе кадра фильтр стартует с нуля и даёт
щелчок ровно как это описано для Resampler.

R посчитан из желаемой частоты среза приближением R = 1 - 2*pi*fc/fs,
корректным при fc << fs (40 Гц против 16-48 кГц -- запас на три порядка).
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["DcBlocker"]


class DcBlocker:
    """HPF первого порядка с переносимым между кадрами состоянием."""

    def __init__(self, sample_rate: int, cutoff_hz: float = 40.0) -> None:
        """Создать фильтр на заданную частоту дискретизации и среза."""
        self._r = 1.0 - (2.0 * math.pi * cutoff_hz / sample_rate)
        self._prev_x = 0.0
        self._prev_y = 0.0

    def reset(self) -> None:
        """Сбросить состояние. Звать при разрыве потока (xrun)."""
        self._prev_x = 0.0
        self._prev_y = 0.0

    def process(self, pcm: np.ndarray) -> np.ndarray:
        """Отфильтровать блок int16, сохранив состояние на следующий вызов."""
        if pcm.size == 0:
            return pcm
        x = pcm.astype(np.float64)
        y = self._filter_scipy(x)
        if y is None:
            y = self._filter_naive(x)
        self._prev_x = float(x[-1])
        self._prev_y = float(y[-1])
        return np.clip(y, -32768, 32767).astype(np.int16)

    def _filter_scipy(self, x: np.ndarray) -> np.ndarray | None:
        """Векторизованный путь через scipy.signal.lfilter, если он есть.

        zi подобран так, чтобы первый выход при продолжении потока совпал
        с наивной рекурсией: y[0] = x[0] - prev_x + r*prev_y. Для transposed
        direct form II c b=[1,-1], a=[1,-r] это zi[0] = r*prev_y - prev_x.
        """
        try:
            from scipy.signal import lfilter
        except ImportError:
            return None
        zi = np.array([self._r * self._prev_y - self._prev_x])
        y, _ = lfilter([1.0, -1.0], [1.0, -self._r], x, zi=zi)
        return y

    def _filter_naive(self, x: np.ndarray) -> np.ndarray:
        """Поблочный fallback без scipy. Медленнее, но не требует зависимости."""
        y = np.empty_like(x)
        prev_x, prev_y, r = self._prev_x, self._prev_y, self._r
        for i in range(x.shape[0]):
            cur = x[i] - prev_x + r * prev_y
            y[i] = cur
            prev_x = x[i]
            prev_y = cur
        return y
