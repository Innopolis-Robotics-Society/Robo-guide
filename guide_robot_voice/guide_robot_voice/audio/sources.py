"""Источники аудио: устройство и файл.

FileSource -- не тестовая заглушка, а рабочий режим. Он позволяет прогнать
весь конвейер (VAD, wakeword, ASR, turn detection) на записанном материале
детерминированно и на каждый коммит. Без него false-wake rate измеряется
только вручную на роботе, то есть не измеряется.

Разрешение устройства по имени, а не по индексу -- отдельная тема.
На роботе несколько capture-интерфейсов: массив, веб-камеры, Kinect.
Порядок перечисления ALSA меняется между загрузками, поэтому hw:1,0
и default одинаково ненадёжны: после перезагрузки ASR слушает веб-камеру,
и это выглядит как "модель вдруг стала плохой". Устройство ищется
подстрокой имени, а отсутствие совпадения -- ошибка конфигурации,
а не повод молча взять default.
"""

from __future__ import annotations

import queue
import threading
import time
import wave
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from guide_robot_voice.audio.devices import resolve_device

__all__ = ["AudioFrame", "AudioSource", "DeviceSource", "FileSource"]

BYTES_PER_SAMPLE = 2


@dataclass(frozen=True)
class AudioFrame:
    """Кадр захвата."""

    pcm: np.ndarray
    """int16, форма (frames,) для моно или (frames, channels)."""

    first_sample: int
    """Индекс первого сэмпла от старта потока. Разрыв означает пропуск буфера."""

    capture_time: float
    """monotonic-время постановки кадра в очередь."""


class AudioSource(Protocol):
    """Источник кадров PCM."""

    sample_rate: int
    channels: int

    def start(self) -> None:
        """Начать захват."""

    def read(self, timeout: float = 1.0) -> AudioFrame | None:
        """Получить следующий кадр или None по таймауту."""

    def stop(self) -> None:
        """Остановить захват."""


class DeviceSource:
    """Захват с ALSA-устройства через sounddevice.

    Про channel_map. XVF3800 в USB-режиме отдаёт хосту два канала:
    левый -- выход после AEC, beamforming и постобработки, правый -- поток
    для ASR с автоматически выбранного луча. Это разные сигналы, и брать
    надо осознанно: для ASR правый, для диагностики и записи ERLE левый.
    """

    def __init__(
        self,
        device: str | int | None = None,
        sample_rate: int = 16000,
        channels: int = 2,
        block_ms: int = 20,
        asr_channel: int = 1,
        monitor_channel: int = 0,
        queue_depth: int = 32,
    ) -> None:
        """Создать источник, не открывая устройство."""
        self.sample_rate = sample_rate
        self.channels = channels
        self._blocksize = int(sample_rate * block_ms / 1000)
        self._device = resolve_device(device, "input", min_channels=channels)
        self._asr_channel = asr_channel
        self._monitor_channel = monitor_channel
        self._queue: queue.Queue[AudioFrame] = queue.Queue(maxsize=queue_depth)
        self._stream: object | None = None
        self._sample_index = 0
        self.overflows = 0
        """Счётчик переполнений. Ненулевой -- значит потребитель не успевает."""

    def start(self) -> None:
        """Открыть и запустить поток захвата."""
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            del time_info
            if status:
                self.overflows += 1
            frame = AudioFrame(
                pcm=np.array(indata, dtype=np.int16, copy=True),
                first_sample=self._sample_index,
                capture_time=time.monotonic(),
            )
            self._sample_index += frames
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                self.overflows += 1

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=callback,
        )
        self._stream.start()  # type: ignore[attr-defined]

    def read(self, timeout: float = 1.0) -> AudioFrame | None:
        """Получить кадр из очереди."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        """Остановить и закрыть поток."""
        if self._stream is not None:
            self._stream.stop()  # type: ignore[attr-defined]
            self._stream.close()  # type: ignore[attr-defined]
            self._stream = None

    def select_asr(self, frame: AudioFrame) -> np.ndarray:
        """Выделить канал, предназначенный для распознавания."""
        return self._select(frame, self._asr_channel)

    def select_monitor(self, frame: AudioFrame) -> np.ndarray:
        """Выделить канал для диагностики и записи."""
        return self._select(frame, self._monitor_channel)

    @staticmethod
    def _select(frame: AudioFrame, channel: int) -> np.ndarray:
        if frame.pcm.ndim == 1:
            return frame.pcm
        return np.ascontiguousarray(frame.pcm[:, channel])


class FileSource:
    """Проигрывание WAV в реальном времени как будто это микрофон.

    realtime=True держит темп, чтобы латентностные метрики имели смысл.
    realtime=False гонит на максимальной скорости -- режим CI, где важна
    только детерминированность выхода детекторов.
    """

    def __init__(
        self,
        path: str,
        block_ms: int = 20,
        realtime: bool = True,
        loop: bool = False,
    ) -> None:
        """Открыть WAV и подготовить чтение."""
        self._path = path
        self._realtime = realtime
        self._loop = loop
        with wave.open(path, "rb") as handle:
            self.sample_rate = handle.getframerate()
            self.channels = handle.getnchannels()
            if handle.getsampwidth() != BYTES_PER_SAMPLE:
                raise ValueError(f"{path}: ожидается 16 бит на сэмпл")
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype=np.int16)
        self._data = data.reshape(-1, self.channels) if self.channels > 1 else data
        self._blocksize = int(self.sample_rate * block_ms / 1000)
        self._position = 0
        self._sample_index = 0
        self._started_at = 0.0
        self._stopped = threading.Event()

    def start(self) -> None:
        """Начать отсчёт времени воспроизведения."""
        self._started_at = time.monotonic()
        self._stopped.clear()

    def read(self, timeout: float = 1.0) -> AudioFrame | None:
        """Выдать следующий кадр, при необходимости выдержав темп."""
        del timeout
        if self._stopped.is_set():
            return None
        if self._position >= len(self._data):
            if not self._loop:
                return None
            self._position = 0

        block = self._data[self._position : self._position + self._blocksize]
        self._position += self._blocksize

        if self._realtime:
            target = self._started_at + self._sample_index / self.sample_rate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        frame = AudioFrame(
            pcm=np.array(block, dtype=np.int16, copy=True),
            first_sample=self._sample_index,
            capture_time=time.monotonic(),
        )
        self._sample_index += len(block)
        return frame

    def stop(self) -> None:
        """Прекратить выдачу кадров."""
        self._stopped.set()
