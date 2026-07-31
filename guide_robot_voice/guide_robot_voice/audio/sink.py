"""Воспроизведение PCM с fencing по epoch и жёсткой отменой.

Требование <200 мс на /speech/cancel_all распадается на две независимые
величины, и путать их нельзя:

  t_stop     publish cancel_all -> тишина в динамике.  Цель <100 мс.
             Определяется глубиной буфера устройства, а не скоростью софта.

  t_bargein  онсет речи человека -> тишина.  Цель <400 мс.
             Определяется детектором, к этому модулю отношения не имеет.

ПОЧЕМУ CALLBACK, А НЕ БЛОКИРУЮЩАЯ ЗАПИСЬ

Первая версия держала писательский поток, который звал stream.write()
и прерывался через abort(). Это оказалось неверным выбором примитива,
и на реальном железе оно развалилось тремя способами сразу: abort()
не всегда разблокирует write(); перезапуск только что прерванного потока
роняет PortAudio в ошибки mmap_begin/SetUpBuffers; закрытие устройства,
внутри которого сидит чужой поток, даёт порчу кучи.

Причина не в частностях. Блокирующие ReadStream/WriteStream в ALSA-бэкенде
PortAudio -- отдельная и заметно менее обкатанная реализация, чем
callback-путь, а главное: делать прерываемость на примитиве, который
по определению не прерывается, -- это бороться со средством вместо
того, чтобы сменить его.

Здесь потока нет вообще. PortAudio сам зовёт _pull() из своего потока,
когда ему нужны кадры. Отсюда:

  * некого join'ить и нечему зависнуть при закрытии;
  * abort() -- штатный Pa_AbortStream, а не попытка прервать чужой write();
  * "чанк в полёте" сжался до одного периода колбэка.

ИНВАРИАНТ

  ПОСЛЕ ВОЗВРАТА bump() НИ ОДИН СЭМПЛ СО СТАРЫМ EPOCH НЕ БУДЕТ ВЫДАН
  В _pull(). В устройстве остаётся не более одного уже заполненного
  периода, который снимает abort().

Отсюда параметр block_ms: длительность периода -- верхняя граница
остатка. 20 мс укладывается в бюджет t_stop, 200 мс -- нет.

Эмиттер вынесен за интерфейс Emitter, чтобы вся логика fencing
тестировалась в CI без звуковой карты.
"""

from __future__ import annotations

import collections
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from guide_robot_voice.audio.devices import resolve_device

__all__ = [
    "Emitter",
    "EpochFencedSink",
    "MemoryEmitter",
    "PullCallable",
    "SharedPluginError",
    "SinkFailureError",
    "SoundDeviceEmitter",
    "StopMetrics",
]

PullCallable = Callable[[int], np.ndarray]
"""Запрос N кадров моно int16. Обязан вернуть ровно N, добивая нулями."""

# Плагины libasound, за которыми стоит звуковой сервер. Через них
# невозможно ни предсказуемое abort(), ни осмысленное измерение задержки.
SHARED_ALSA_PLUGINS = frozenset({"pulse", "default", "sysdefault", "dmix", "pipewire"})


class SinkFailureError(RuntimeError):
    """Отказ вывода. Всё, что сток сообщает после этого, недостоверно."""


class SharedPluginError(ValueError):
    """Попытка открыть вывод через звуковой сервер."""


class Emitter(Protocol):
    """Приёмник PCM. Абстракция над устройством вывода."""

    def open(self, pull: PullCallable) -> None:
        """Захватить устройство и начать запрашивать кадры."""

    def abort(self) -> None:
        """Немедленно остановить вывод, сбросив буфер устройства."""

    def resume(self) -> None:
        """Возобновить вывод после abort()."""

    def close(self) -> None:
        """Освободить устройство."""

    def info(self) -> dict[str, object]:
        """Фактические параметры открытого потока."""


@dataclass
class StopMetrics:
    """Телеметрия последней отмены, уходит в /diagnostics."""

    epoch: int = 0
    reason: str = ""
    requested_at: float = 0.0
    aborted_at: float = 0.0
    dropped_chunks: int = 0
    dropped_frames: int = 0

    @property
    def t_stop_ms(self) -> float:
        """Софтовая часть t_stop: от вызова bump() до возврата abort()."""
        return (self.aborted_at - self.requested_at) * 1e3


@dataclass
class _Chunk:
    epoch: int
    pcm: np.ndarray


@dataclass
class _State:
    epoch: int = 0
    cursor: int = 0
    queued_frames: int = 0
    played_frames: int = 0
    chunks: collections.deque[_Chunk] = field(default_factory=collections.deque)


class MemoryEmitter:
    """Эмиттер для тестов: имитирует поток колбэков без звуковой карты."""

    def __init__(self, block: int = 320, interval: float = 0.001) -> None:
        """Создать эмиттер с заданным периодом колбэка."""
        self.writes: list[np.ndarray] = []
        self.abort_marks: list[int] = []
        self._block = block
        self._interval = interval
        self._pull: PullCallable | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._active = threading.Event()
        self._lock = threading.Lock()

    def open(self, pull: PullCallable) -> None:
        """Запустить имитацию колбэков."""
        self._pull = pull
        self._running.set()
        self._active.set()
        self._thread = threading.Thread(target=self._loop, name="memory-emitter", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running.is_set():
            if not self._active.is_set():
                time.sleep(self._interval)
                continue
            assert self._pull is not None
            block = self._pull(self._block)
            with self._lock:
                self.writes.append(block)
            time.sleep(self._interval)

    def abort(self) -> None:
        """Остановить выдачу и отметить точку сброса."""
        self._active.clear()
        with self._lock:
            self.abort_marks.append(len(self.writes))

    def resume(self) -> None:
        """Возобновить выдачу."""
        self._active.set()

    def close(self) -> None:
        """Остановить поток имитации."""
        self._running.clear()
        self._active.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def info(self) -> dict[str, object]:
        """Параметры имитации."""
        return {"name": "memory", "blocksize": self._block}


class SoundDeviceEmitter:
    """Эмиттер поверх ALSA через sounddevice в callback-режиме.

    buffer_ms -- нижняя граница t_stop на реальном железе. Финальное
    значение подбирается по числу underrun'ов на роботе, а не угадывается.
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int = 2,
        block_ms: int = 20,
        buffer_ms: int = 80,
        device: str | int | None = None,
        allow_shared: bool = False,
    ) -> None:
        """Создать эмиттер и разрешить устройство немедленно."""
        import sounddevice as sd

        self._sd = sd
        self._sample_rate = sample_rate
        self._channels = channels
        self._blocksize = int(sample_rate * block_ms / 1000)
        self._latency = buffer_ms / 1000.0
        # Резолв здесь, а не при первом кадре: отказ устройства обязан
        # прилететь вызывающему, а не всплыть внутри аудиопотока.
        self._device = resolve_device(device, "output", min_channels=channels)
        self._reject_shared_plugin(allow_shared)
        self._stream: object | None = None
        self._pull: PullCallable | None = None
        self._failure: BaseException | None = None
        self._lock = threading.Lock()

    def _reject_shared_plugin(self, allow_shared: bool) -> None:
        """Отказаться работать через звуковой сервер, если явно не разрешено."""
        if self._device is None or allow_shared:
            return
        name = self._sd.query_devices(self._device)["name"].split(":")[0].strip().lower()
        if name not in SHARED_ALSA_PLUGINS:
            return
        raise SharedPluginError(
            f"устройство {name!r} -- разделяемый плагин ALSA поверх звукового сервера.\n"
            "Ни корректного abort(), ни предсказуемой задержки на этом пути нет.\n"
            "Открывайте железо напрямую, освободив его от сервера:\n"
            "  pasuspender -- <команда>   ... --device hw:<карта>,0\n"
            "Осознанно работать через сервер: allow_shared=True (--allow-shared)."
        )

    def _callback(self, outdata, frames: int, time_info, status) -> None:  # noqa: ANN001
        """Колбэк PortAudio. Обязан быть коротким и не бросать наружу."""
        del time_info, status
        try:
            assert self._pull is not None
            mono = self._pull(frames)
            outdata[:] = mono[:, None] if self._channels > 1 else mono.reshape(-1, 1)
        except BaseException as error:
            self._failure = error
            outdata.fill(0)

    def open(self, pull: PullCallable) -> None:
        """Открыть устройство и запустить поток колбэков."""
        self._pull = pull
        with self._lock:
            self._stream = self._sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                blocksize=self._blocksize,
                latency=self._latency,
                device=self._device,
                callback=self._callback,
            )
            self._stream.start()  # type: ignore[attr-defined]

    def abort(self) -> None:
        """Pa_AbortStream: остановить немедленно, буфер устройства выбросить."""
        stream = self._stream
        if stream is not None:
            with contextlib.suppress(self._sd.PortAudioError):
                stream.abort()  # type: ignore[attr-defined]

    def resume(self) -> None:
        """Запустить поток заново после abort()."""
        stream = self._stream
        if stream is None:
            return
        with contextlib.suppress(self._sd.PortAudioError):
            if not stream.active:  # type: ignore[attr-defined]
                stream.start()  # type: ignore[attr-defined]

    def close(self) -> None:
        """Закрыть устройство."""
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(self._sd.PortAudioError):
                stream.close()  # type: ignore[attr-defined]

    @property
    def failure(self) -> BaseException | None:
        """Исключение, вылетевшее из колбэка."""
        return self._failure

    @property
    def latency(self) -> float:
        """Заявленная задержка вывода, сек."""
        stream = self._stream
        value = getattr(stream, "latency", self._latency)
        return float(value) if isinstance(value, (int, float)) else self._latency

    def info(self) -> dict[str, object]:
        """Фактические параметры открытого потока.

        Печатать перед любым измерением: аргумент --device и устройство,
        на котором в итоге пошёл звук, -- разные сущности.
        """
        stream = self._stream
        device = self._sd.query_devices(self._device) if self._device is not None else None
        return {
            "index": self._device,
            "name": device["name"] if device else "default",
            "samplerate": getattr(stream, "samplerate", None),
            "blocksize": getattr(stream, "blocksize", None),
            "channels": self._channels,
            "latency": getattr(stream, "latency", None),
            "active": getattr(stream, "active", False),
        }


class EpochFencedSink:
    """Очередь воспроизведения с отбрасыванием устаревших чанков."""

    def __init__(
        self,
        emitter: Emitter,
        sample_rate: int,
        max_queue_ms: int = 600,
    ) -> None:
        """Создать сток. Устройство открывается методом start()."""
        self._emitter = emitter
        self._sample_rate = sample_rate
        self._max_queue_frames = int(sample_rate * max_queue_ms / 1000)
        self._state = _State()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._closed = False
        self._opened = False
        self._metrics = StopMetrics()
        self.underflows = 0
        """Число колбэков, которым не хватило данных. Рост -- синтез не успевает."""

    # -- жизненный цикл -----------------------------------------------------

    def start(self) -> None:
        """Открыть устройство. Отказ поднимается здесь, а не позже."""
        if self._opened:
            return
        self._emitter.open(self._pull)
        self._opened = True

    def close(self) -> None:
        """Остановить вывод и освободить устройство."""
        with self._cv:
            self._closed = True
            self._state.chunks.clear()
            self._state.queued_frames = 0
            self._state.cursor = 0
            self._cv.notify_all()
        self._emitter.abort()
        self._emitter.close()
        self._opened = False

    # -- состояние ----------------------------------------------------------

    @property
    def epoch(self) -> int:
        """Текущий epoch."""
        with self._lock:
            return self._state.epoch

    @property
    def metrics(self) -> StopMetrics:
        """Телеметрия последней отмены."""
        with self._lock:
            return self._metrics

    @property
    def failure(self) -> BaseException | None:
        """Отказ вывода, если он был."""
        return getattr(self._emitter, "failure", None)

    def raise_if_failed(self) -> None:
        """Поднять отказ вывода в вызывающем потоке.

        Вызывать перед тем, как сообщать любые измерения: сток
        с неработающим устройством честно отработает всю логику fencing
        и выдаст правдоподобные миллисекунды при нулевом звуке.
        """
        failure = self.failure
        if failure is not None:
            raise SinkFailureError("вывод отказал") from failure

    def pending_seconds(self) -> float:
        """Сколько аудио в очереди, не считая буфера устройства."""
        with self._lock:
            return self._state.queued_frames / self._sample_rate

    def played_seconds(self) -> float:
        """Сколько аудио выдано в устройство с момента последнего bump()."""
        with self._lock:
            return self._state.played_frames / self._sample_rate

    # -- продюсер -----------------------------------------------------------

    def submit(self, epoch: int, pcm: np.ndarray, timeout: float = 30.0) -> bool:
        """Поставить чанк в очередь.

        False означает "прекрати синтез": либо epoch устарел, либо сток
        закрыт, либо вывод отказал. Продолжать генерировать после этого --
        значит жечь время на аудио, которое некуда девать.
        """
        if self.failure is not None:
            return False
        frames = int(pcm.shape[0])
        deadline = time.monotonic() + timeout
        with self._cv:
            while (
                not self._closed
                and epoch == self._state.epoch
                and self._state.queued_frames + frames > self._max_queue_frames
                and self._state.chunks
            ):
                if time.monotonic() > deadline:
                    return False
                self._cv.wait(timeout=0.05)
            if self._closed or epoch != self._state.epoch:
                return False
            self._state.chunks.append(_Chunk(epoch=epoch, pcm=pcm))
            self._state.queued_frames += frames
            self._cv.notify_all()
        self._emitter.resume()
        return True

    # -- отмена -------------------------------------------------------------

    def bump(self, reason: str = "") -> int:
        """Отменить всё воспроизведение и вернуть новый epoch.

        Порядок существенен. Сначала под локом инкрементируется epoch
        и чистится очередь -- с этого момента колбэк физически не может
        выдать устаревший сэмпл. Только потом рвётся буфер устройства.
        """
        requested_at = time.monotonic()
        with self._cv:
            dropped_chunks = len(self._state.chunks)
            dropped_frames = self._state.queued_frames
            self._state.epoch += 1
            new_epoch = self._state.epoch
            self._state.chunks.clear()
            self._state.queued_frames = 0
            self._state.cursor = 0
            self._state.played_frames = 0
            self._cv.notify_all()

        self._emitter.abort()
        aborted_at = time.monotonic()

        with self._lock:
            self._metrics = StopMetrics(
                epoch=new_epoch,
                reason=reason,
                requested_at=requested_at,
                aborted_at=aborted_at,
                dropped_chunks=dropped_chunks,
                dropped_frames=dropped_frames,
            )
        return new_epoch

    # -- завершение ---------------------------------------------------------

    def wait_idle(self, epoch: int, timeout: float = 30.0) -> bool:
        """Дождаться, пока очередь опустеет и устройство доиграет.

        False -- epoch устарел (нас отменили), сток закрыт или истёк таймаут.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self._closed or epoch != self._state.epoch:
                    return False
                if not self._state.chunks and self._state.cursor == 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=min(remaining, 0.05))

        # Очередь пуста, но в устройстве ещё лежит буфер. Ждём его выдачу.
        latency = getattr(self._emitter, "latency", 0.0)
        time.sleep(min(float(latency) + 0.02, max(0.0, deadline - time.monotonic())))
        return self.epoch == epoch

    # -- колбэк -------------------------------------------------------------

    def _pull(self, frames: int) -> np.ndarray:
        """Выдать ровно frames кадров, добивая нулями.

        Вызывается из аудиопотока PortAudio. Никаких блокировок сверх
        одного короткого лока и никаких аллокаций сверх одного буфера.
        """
        out = np.zeros(frames, dtype=np.int16)
        filled = 0
        with self._lock:
            epoch = self._state.epoch
            while filled < frames and self._state.chunks:
                chunk = self._state.chunks[0]
                if chunk.epoch != epoch:
                    # Fencing. До сюда доходить не должно -- bump() чистит
                    # очередь, -- но проверка стоит один if на период.
                    self._state.queued_frames -= chunk.pcm.shape[0] - self._state.cursor
                    self._state.chunks.popleft()
                    self._state.cursor = 0
                    continue
                available = int(chunk.pcm.shape[0]) - self._state.cursor
                take = min(available, frames - filled)
                out[filled : filled + take] = chunk.pcm[
                    self._state.cursor : self._state.cursor + take
                ]
                filled += take
                self._state.cursor += take
                self._state.queued_frames -= take
                if self._state.cursor >= chunk.pcm.shape[0]:
                    self._state.chunks.popleft()
                    self._state.cursor = 0
            self._state.played_frames += filled
            if filled < frames:
                self.underflows += 1
            self._cv.notify_all()
        return out
