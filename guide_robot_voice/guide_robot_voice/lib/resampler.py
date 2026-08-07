"""Приведение частоты дискретизации между моделью и устройством.

Зачем отдельный модуль. Голоса Piper для русского работают на 22050 Гц,
Silero VAD -- на 16000 (кадрами по 512 сэмплов), а устройство, открытое
через hw:, не выполняет никаких преобразований и требует ровно ту частоту,
которую объявило железо. Типичный USB Audio Class объявляет 48000 и больше
ничего.

Обходной путь через plughw: существует, но прячет преобразование внутрь
ALSA, где его не видно ни в логах, ни в диагностике. На роботе это станет
источником вопросов вида "почему голос звенит": plughw по умолчанию берёт
самый дешёвый конвертер. Лучше делать это явно и знать, каким фильтром.

scipy.signal.resample_poly -- правильный полифазный ресемплер с фильтром
против наложения спектров. Если scipy нет, используется линейная
интерполяция: для повышения частоты речи она приемлема, но добавляет
высокочастотные артефакты, о чём модуль честно сообщает через uses_scipy.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["Resampler", "resample_int16"]


def _polyphase(pcm: np.ndarray, up: int, down: int) -> np.ndarray | None:
    try:
        from scipy.signal import resample_poly
    except ImportError:
        return None
    result = resample_poly(pcm.astype(np.float32), up, down)
    return np.clip(result, -32768, 32767).astype(np.int16)


def _linear(pcm: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if pcm.size == 0:
        return pcm
    count = round(pcm.shape[0] * target_rate / source_rate)
    if count <= 0:
        return np.zeros(0, dtype=np.int16)
    source_index = np.arange(pcm.shape[0], dtype=np.float64)
    target_index = np.linspace(0.0, pcm.shape[0] - 1, count)
    return np.interp(target_index, source_index, pcm.astype(np.float64)).astype(np.int16)


def resample_int16(pcm: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Пересчитать моно int16 к целевой частоте."""
    if source_rate == target_rate or pcm.size == 0:
        return pcm
    divisor = math.gcd(source_rate, target_rate)
    result = _polyphase(pcm, target_rate // divisor, source_rate // divisor)
    return result if result is not None else _linear(pcm, source_rate, target_rate)


class Resampler:
    """Пересчёт частоты с сохранением остатка между кадрами.

    Потоковый ресемплинг покадрово без сохранения состояния даёт щелчок
    на каждой границе кадра: фильтр каждый раз стартует с нуля. Здесь
    хвост предыдущего кадра переносится в следующий вызов.
    """

    def __init__(self, source_rate: int, target_rate: int, overlap: int = 64) -> None:
        """Создать ресемплер. Совпадающие частоты дают проход насквозь."""
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.passthrough = source_rate == target_rate
        self._overlap = 0 if self.passthrough else overlap
        self._tail = np.zeros(0, dtype=np.int16)

    @property
    def uses_scipy(self) -> bool:
        """Доступен ли полифазный ресемплер."""
        try:
            import scipy.signal  # noqa: F401
        except ImportError:
            return False
        return True

    def reset(self) -> None:
        """Сбросить накопленный хвост. Звать при bump() epoch."""
        self._tail = np.zeros(0, dtype=np.int16)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        """Пересчитать очередной кадр."""
        if self.passthrough:
            return pcm
        padded = np.concatenate([self._tail, pcm]) if self._tail.size else pcm
        self._tail = pcm[-self._overlap :].copy() if pcm.size >= self._overlap else pcm.copy()

        converted = resample_int16(padded, self.source_rate, self.target_rate)
        if not self._tail.size or padded.shape[0] == pcm.shape[0]:
            return converted
        # Отрезать ту часть выхода, что соответствует перенесённому хвосту.
        skip = round((padded.shape[0] - pcm.shape[0]) * self.target_rate / self.source_rate)
        return converted[skip:]
