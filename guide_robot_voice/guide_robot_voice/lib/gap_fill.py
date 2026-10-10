"""Заполнение коротких пропусков в /audio/mic тишиной.

Под нагрузкой CPU до VAD/ASR не доходят отдельные блоки по 16-80 мс. Раньше
любой такой пропуск считался разрывом захвата: voice_session_manager и
asr_node выбрасывали всю открытую фразу (живой случай: распознанная «Фирая»
потерялась вместе с командой). Короткая дыра внутри той же сессии захвата
безопаснее как тишина: фраза сохраняется, ASR теряет от силы звук. Длинные
разрывы и смена сессии по-прежнему остаются разрывами.

Модуль без зависимостей от rclpy: тестируется в CI как обычная функция.
"""

from __future__ import annotations

import numpy as np

__all__ = ["fill_small_gap"]


def fill_small_gap(
    expected_first_sample: int | None,
    first_sample: int,
    samples: np.ndarray,
    max_fill_samples: int,
) -> tuple[int, np.ndarray, int]:
    """Вернуть (first_sample, samples, заполнено) с тишиной на месте короткого пропуска.

    Заполняется только пропуск вперёд, не длиннее `max_fill_samples`; иначе
    блок возвращается как есть и вызывающий обрабатывает разрыв по-старому.
    Сессию захвата сверяет вызывающий: между сессиями счётчики несравнимы.
    """
    if expected_first_sample is None or max_fill_samples <= 0:
        return first_sample, samples, 0
    gap = first_sample - expected_first_sample
    if gap <= 0 or gap > max_fill_samples:
        return first_sample, samples, 0
    filled = np.concatenate([np.zeros(gap, dtype=np.int16), samples.astype(np.int16, copy=False)])
    return expected_first_sample, filled, gap
