"""Измерение акустического t_stop через петлю на одной карте.

ЧТО ИЗМЕРЯЕМ

Софтовый t_stop (от bump() до возврата abort()) -- доли миллисекунды
и ни о чём не говорит. Настоящий вопрос: выбрасывает ли abort() то, что
уже лежит в буфере устройства, или эти 80 мс доигрывают. Разница между
"уложились в бюджет" и "не уложились".

ПОЧЕМУ ЗАХВАТ ОТДЕЛЬНЫМ ПОТОКОМ

Первая версия открывала один дуплексный поток и звала на нём abort().
Это тупик по построению: abort() останавливает поток целиком, вместе
с захватом. То есть ровно в тот момент, ради наблюдения за которым всё
и затевалось, запись прекращается, и хвост звука наблюдать нечем.

Здесь два независимых потока на одной карте: выход, который отменяется,
и вход, который пишет непрерывно и переживает отмену. Карта одна,
значит тактовый генератор общий, и счётчики кадров не разъезжаются.

ПРОТОКОЛ

Одна запись, одна отмена в самом конце.

  1. пауза -- переходный процесс открытия потоков наружу не выносим
  2. калибровочный всплеск шума, потом тишина
  3. непрерывный тон
  4. bump(), запоминаем frames_out
  5. дописываем хвост: выход уже мёртв, вход ещё пишет

Смещение между осями выхода и входа берётся взаимной корреляцией
огибающих на калибровочном участке. Порог по первому громкому кадру
для этого не годится: открытие потока ALSA само даёт щелчок во входе,
и детектор цепляется за него. Шумовой всплеск выбран потому, что его
огибающая даёт однозначный максимум корреляции.

  t_stop = last_loud(вход) - смещение - frames_out_на_момент_отмены

Интерпретация:

  ~ один период колбэка   ->  abort() выбросил буфер, бюджет выполним
  ~ buffer_ms             ->  буфер доиграл, резать buffer_ms
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from guide_robot_voice.audio.devices import resolve_device
from guide_robot_voice.audio.sink import PullCallable

__all__ = [
    "LoopbackEmitter",
    "LoopbackResult",
    "Recording",
    "analyse_stop",
    "envelope",
    "estimate_offset",
    "noise_burst",
]

ENVELOPE_BLOCK = 64
THRESHOLD_FRACTION = 0.15


def envelope(signal: np.ndarray, block: int = ENVELOPE_BLOCK) -> np.ndarray:
    """Поблочный RMS. Устойчивее к отдельным выбросам, чем модуль сэмпла."""
    usable = (signal.shape[0] // block) * block
    if usable == 0:
        return np.zeros(0, dtype=np.float64)
    reshaped = signal[:usable].astype(np.float64).reshape(-1, block)
    return np.sqrt(np.mean(np.square(reshaped), axis=1))


def _last_loud(signal: np.ndarray, block: int = ENVELOPE_BLOCK) -> int | None:
    env = envelope(signal, block)
    if env.size == 0 or env.max() <= 0:
        return None
    loud = np.flatnonzero(env >= THRESHOLD_FRACTION * env.max())
    return int((loud[-1] + 1) * block) if loud.size else None


def noise_burst(frames: int, amplitude: int = 12000, seed: int = 7) -> np.ndarray:
    """Всплеск белого шума с плавными краями.

    Шум, а не тон: его огибающая даёт однозначный максимум взаимной
    корреляции, тогда как тон легко спутать с основным сигналом,
    если тот на той же частоте.
    """
    generator = np.random.default_rng(seed)
    signal = generator.normal(0.0, amplitude / 3.0, frames)
    ramp = min(frames // 8, 128)
    if ramp > 0:
        window = np.hanning(2 * ramp)
        signal[:ramp] *= window[:ramp]
        signal[-ramp:] *= window[ramp:]
    return np.clip(signal, -32767, 32767).astype(np.int16)


@dataclass(frozen=True)
class Recording:
    """Дорожки одного прогона.

    out растёт только пока жив поток вывода и обрывается на abort().
    inp пишется непрерывно и продолжается после него -- в этом весь смысл.
    """

    out: np.ndarray
    inp: np.ndarray


@dataclass(frozen=True)
class LoopbackResult:
    """Результат измерения."""

    offset_frames: int
    mark_out: int
    stopped_at: int
    sample_rate: int
    warnings: tuple[str, ...] = ()

    @property
    def t_stop_ms(self) -> float:
        """Сколько звучало после отмены, мс."""
        return (self.stopped_at - self.mark_out) / self.sample_rate * 1e3

    @property
    def offset_ms(self) -> float:
        """Смещение оси входа относительно оси выхода, мс."""
        return self.offset_frames / self.sample_rate * 1e3

    @property
    def valid(self) -> bool:
        """Можно ли доверять числу."""
        return not self.warnings


def estimate_offset(
    recording: Recording,
    sample_rate: int,
    guard_ms: float = 150.0,
    window_ms: float = 1200.0,
    max_lag_ms: float = 500.0,
    block: int = ENVELOPE_BLOCK,
) -> int:
    """Смещение оси входа относительно выхода по калибровочному участку.

    Окно ограничивается началом записи, где кроме калибровочного всплеска
    ничего нет: иначе корреляция цепляется за основной тон.
    """
    guard = int(sample_rate * guard_ms / 1000)
    window = int(sample_rate * window_ms / 1000)
    out_env = envelope(recording.out[guard : guard + window], block)
    in_env = envelope(recording.inp[guard : guard + window], block)
    if out_env.size == 0 or in_env.size == 0:
        raise ValueError("запись короче калибровочного окна")
    if out_env.max() <= 0:
        raise ValueError(
            "калибровочный всплеск не найден в выходе. Он должен попадать "
            f"в окно {guard_ms:.0f}..{guard_ms + window_ms:.0f} мс от старта потока"
        )
    if in_env.max() <= 0:
        raise ValueError("во входе тишина. Наушник прижат к микрофону? Громкость не в нуле?")

    out_env = out_env - out_env.mean()
    in_env = in_env - in_env.mean()
    max_lag = int(sample_rate * max_lag_ms / 1000 / block)
    correlation = np.correlate(in_env, out_env, mode="full")
    zero_lag = out_env.size - 1
    tail = correlation[zero_lag : zero_lag + max_lag + 1]
    if tail.size == 0:
        raise ValueError("окно корреляции пустое: увеличьте max_lag_ms")
    return int(np.argmax(tail)) * block


def analyse_stop(
    recording: Recording,
    mark_out: int,
    sample_rate: int,
    block: int = ENVELOPE_BLOCK,
    guard_ms: float = 150.0,
) -> LoopbackResult:
    """Посчитать t_stop по одной записи с калибровочным участком."""
    offset = estimate_offset(recording, sample_rate, guard_ms=guard_ms, block=block)

    stop_in = _last_loud(recording.inp, block)
    if stop_in is None:
        raise ValueError("во входе нет сигнала: тон не дошёл до микрофона")
    stopped_at = stop_in - offset

    warnings: list[str] = []
    if offset < block * 2:
        warnings.append(
            f"смещение {offset} кадров подозрительно мало: корреляция могла "
            "зацепиться за наводку, а не за акустику"
        )
    if stopped_at < mark_out - block:
        early_ms = (mark_out - stopped_at) / sample_rate * 1e3
        warnings.append(f"обрыв найден раньше отмены на {early_ms:.0f} мс: измерение неверно")
    return LoopbackResult(
        offset_frames=offset,
        mark_out=mark_out,
        stopped_at=stopped_at,
        sample_rate=sample_rate,
        warnings=tuple(warnings),
    )


class LoopbackEmitter:
    """Выход через сток плюс независимый непрерывный захват.

    Реализует протокол Emitter, поэтому измеряется РЕАЛЬНЫЙ путь
    воспроизведения: тот же EpochFencedSink, та же отмена. Мерить
    отдельной упрощённой реализацией означало бы мерить не то, что работает.
    """

    def __init__(
        self,
        sample_rate: int,
        out_channels: int = 2,
        in_channels: int = 1,
        block_ms: int = 20,
        buffer_ms: int = 80,
        device: str | int | None = None,
    ) -> None:
        """Разрешить устройство в обе стороны и подготовить буферы."""
        import sounddevice as sd

        self._sd = sd
        self._sample_rate = sample_rate
        self._out_channels = out_channels
        self._in_channels = in_channels
        self._blocksize = int(sample_rate * block_ms / 1000)
        self._latency = buffer_ms / 1000.0
        self._out_device = resolve_device(device, "output", min_channels=out_channels)
        self._in_device = resolve_device(device, "input", min_channels=in_channels)
        self._out_stream: object | None = None
        self._in_stream: object | None = None
        self._pull: PullCallable | None = None
        self._failure: BaseException | None = None
        self._lock = threading.Lock()
        self._recorded_in: list[np.ndarray] = []
        self._recorded_out: list[np.ndarray] = []
        self.frames_out = 0
        """Кадров отдано колбэком вывода. Снимок в момент bump() -- это mark_out."""

    def _out_callback(self, outdata, frames: int, time_info, status) -> None:  # noqa: ANN001
        del time_info, status
        try:
            assert self._pull is not None
            mono = self._pull(frames)
            outdata[:] = np.repeat(mono[:, None], self._out_channels, axis=1)
            with self._lock:
                self._recorded_out.append(mono.copy())
                self.frames_out += frames
        except BaseException as error:
            self._failure = error
            outdata.fill(0)

    def _in_callback(self, indata, frames: int, time_info, status) -> None:  # noqa: ANN001
        del frames, time_info, status
        with self._lock:
            self._recorded_in.append(np.array(indata[:, 0], copy=True))

    def open(self, pull: PullCallable) -> None:
        """Открыть оба потока. Захват первым, чтобы не потерять начало."""
        self._pull = pull
        self._in_stream = self._sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._in_channels,
            dtype="int16",
            blocksize=self._blocksize,
            latency=self._latency,
            device=self._in_device,
            callback=self._in_callback,
        )
        self._in_stream.start()  # type: ignore[attr-defined]

        self._out_stream = self._sd.OutputStream(
            samplerate=self._sample_rate,
            channels=self._out_channels,
            dtype="int16",
            blocksize=self._blocksize,
            latency=self._latency,
            device=self._out_device,
            callback=self._out_callback,
        )
        self._out_stream.start()  # type: ignore[attr-defined]

    def abort(self) -> None:
        """Остановить ТОЛЬКО вывод. Захват обязан пережить отмену."""
        if self._out_stream is not None:
            self._out_stream.abort()  # type: ignore[attr-defined]

    def resume(self) -> None:
        """Запустить вывод заново после abort()."""
        stream = self._out_stream
        if stream is not None and not stream.active:  # type: ignore[attr-defined]
            stream.start()  # type: ignore[attr-defined]

    def close(self) -> None:
        """Закрыть оба потока."""
        for attribute in ("_out_stream", "_in_stream"):
            stream = getattr(self, attribute)
            setattr(self, attribute, None)
            if stream is not None:
                stream.close()

    @property
    def failure(self) -> BaseException | None:
        """Исключение из колбэка вывода."""
        return self._failure

    @property
    def latency(self) -> float:
        """Заявленная задержка, сек."""
        return self._latency

    def take_recording(self) -> Recording:
        """Забрать накопленные дорожки."""
        with self._lock:
            out = list(self._recorded_out)
            inp = list(self._recorded_in)
        empty = np.zeros(0, dtype=np.int16)
        return Recording(
            out=np.concatenate(out) if out else empty,
            inp=np.concatenate(inp) if inp else empty,
        )

    def info(self) -> dict[str, object]:
        """Фактические параметры потоков."""
        return {
            "out_index": self._out_device,
            "in_index": self._in_device,
            "samplerate": getattr(self._out_stream, "samplerate", None),
            "blocksize": getattr(self._out_stream, "blocksize", None),
            "out_active": getattr(self._out_stream, "active", False),
            "in_active": getattr(self._in_stream, "active", False),
        }
