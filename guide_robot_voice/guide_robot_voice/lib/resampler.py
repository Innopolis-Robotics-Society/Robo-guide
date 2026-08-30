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

`soxr.ResampleStream` -- основной путь для потокового `Resampler`: единственный
из доступных, что держит состояние ФИЛЬТРА (не только остаток сэмплов) на
границе блоков. `scipy.signal.resample_poly` фильтрует каждый блок независимо
-- даже с точной бухгалтерией длины (см. `Resampler`) это даёт ~19dB потерь
SNR на стыках блоков, потому что сам фильтр каждый раз стартует заново.
Без `soxr` используется `resample_poly` (полифазный, но без непрерывности
фильтра между блоками), без scipy -- линейная интерполяция: для повышения
частоты речи приемлема, но добавляет высокочастотные артефакты. Модуль
честно сообщает о фактическом бэкенде через `uses_soxr`/`uses_scipy`.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["Resampler", "resample_int16"]


def _soxr_available() -> bool:
    try:
        import soxr  # noqa: F401
    except ImportError:
        return False
    return True


def _scipy_available() -> bool:
    try:
        import scipy.signal  # noqa: F401
    except ImportError:
        return False
    return True


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
    """Пересчитать моно int16 к целевой частоте (разовый вызов, без состояния)."""
    if source_rate == target_rate or pcm.size == 0:
        return pcm
    divisor = math.gcd(source_rate, target_rate)
    result = _polyphase(pcm, target_rate // divisor, source_rate // divisor)
    return result if result is not None else _linear(pcm, source_rate, target_rate)


class Resampler:
    """Потоковый пересчёт частоты с сохранением состояния между кадрами.

    Покадровый ресемплинг без сохранения состояния даёт щелчок на каждой
    границе кадра: фильтр каждый раз стартует с нуля. `soxr.ResampleStream`
    (если доступен) держит состояние фильтра нативно -- предпочтительный
    путь, см. `uses_soxr`.

    Без `soxr` -- запасной путь на `resample_poly`/линейной интерполяции с
    переносом ХВОСТА сэмплов между кадрами (для непрерывности контекста
    фильтра) и точной бухгалтерией длины по АБСОЛЮТНОЙ, не поблочной,
    позиции: `_input_consumed` считает сэмплы оригинального потока с начала
    (без дублирования хвостом), `_ideal_output_length(n)` -- сколько сэмплов
    должно быть на выходе к этой позиции по формуле активного бэкенда
    (`ceil` для `resample_poly`, `round` для линейной интерполяции). Отдаём
    наружу ровно разницу idealного счёта между началом и концом кадра --
    инвариантно к тому, как входной поток порезан на кадры. Это чинит ДЛИНУ
    (0 сэмплов дрейфа), но не полностью SNR -- на стыках блоков всё ещё
    независимая фильтрация каждого блока, отсюда и предпочтение `soxr`.
    """

    def __init__(self, source_rate: int, target_rate: int, overlap: int = 64) -> None:
        """Создать ресемплер. Совпадающие частоты дают проход насквозь."""
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.passthrough = source_rate == target_rate
        self._overlap = 0 if self.passthrough else overlap
        self._tail = np.zeros(0, dtype=np.int16)
        self._use_soxr = _soxr_available() and not self.passthrough
        self._use_scipy = _scipy_available()
        divisor = math.gcd(source_rate, target_rate)
        self._up = target_rate // divisor
        self._down = source_rate // divisor
        self._input_consumed = 0
        self._soxr_stream = self._new_soxr_stream() if self._use_soxr else None

    def _new_soxr_stream(self):  # noqa: ANN202 -- тип из optional-зависимости
        import soxr

        return soxr.ResampleStream(self.source_rate, self.target_rate, 1, dtype="int16")

    @property
    def uses_soxr(self) -> bool:
        """Активен ли стейтфул-полифазный `soxr` (сохраняет фильтр между кадрами)."""
        return self._use_soxr

    @property
    def uses_scipy(self) -> bool:
        """Доступен ли полифазный `resample_poly` (запасной путь без `soxr`)."""
        return self._use_scipy

    def reset(self) -> None:
        """Сбросить состояние (хвост/фильтр) БЕЗ флеша -- для разрывов (xrun/CancelAll).

        В отличие от `flush()` не отдаёт буферизованные сэмплы: разрыв
        (переполнение/барж-ин) значит, что этот хвост звучать не должен.
        """
        self._tail = np.zeros(0, dtype=np.int16)
        self._input_consumed = 0
        if self._use_soxr:
            self._soxr_stream = self._new_soxr_stream()

    def flush(self) -> np.ndarray:
        """Отдать сэмплы, ещё удержанные внутренним буфером `soxr`, на ЧИСТОМ конце потока.

        Звать в конце нормально завершившегося отрезка (последняя клауза
        реплики), НЕ при разрыве -- для разрыва нужен `reset()`. Без
        `soxr` буферизации между кадрами нет (запасной путь уже отдаёт
        каждый кадр целиком), поэтому здесь просто пусто.
        """
        if not self._use_soxr or self.passthrough:
            return np.zeros(0, dtype=np.int16)
        out = self._soxr_stream.resample_chunk(np.zeros(0, dtype=np.int16), last=True)
        self._soxr_stream = self._new_soxr_stream()
        return np.asarray(out, dtype=np.int16)

    def _ideal_output_length(self, input_count: int) -> int:
        """Сколько сэмплов дал бы разовый resample первых `input_count` входных (запасной путь)."""
        if self._use_scipy:
            return math.ceil(input_count * self._up / self._down)
        return round(input_count * self.target_rate / self.source_rate)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        """Пересчитать очередной кадр."""
        if self.passthrough:
            return pcm
        if self._use_soxr:
            return np.asarray(self._soxr_stream.resample_chunk(pcm), dtype=np.int16)

        padded = np.concatenate([self._tail, pcm]) if self._tail.size else pcm
        self._tail = pcm[-self._overlap :].copy() if pcm.size >= self._overlap else pcm.copy()

        converted = resample_int16(padded, self.source_rate, self.target_rate)

        input_before = self._input_consumed
        input_after = input_before + pcm.shape[0]
        self._input_consumed = input_after

        wanted = self._ideal_output_length(input_after) - self._ideal_output_length(input_before)
        skip = converted.shape[0] - wanted
        return converted[skip:] if skip > 0 else converted
