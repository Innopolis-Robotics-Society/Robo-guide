"""Нода захвата звука. Единственный владелец устройства входа.

Больше никто в системе не открывает PCM на вход -- vad_node, wakeword_node
и asr_node подписаны на /audio/mic, а не на устройство напрямую (design §3.1).

Порядок обработки кадра: захват -> downmix в моно -> DC-blocker -> gain ->
ресемплинг device_rate -> out_rate -> нарезка на кадры фиксированной длины
через RingBuffer -> публикация. Ресемплер даёт блоки чуть плавающей длины
(см. lib/resampler.py), поэтому нарезка на ровные кадры для VAD/openWakeWord
обязана быть отдельным шагом, а не совмещаться с ресемплингом.

Про штамп времени. Публикуемый AudioChunk обязан нести время ПЕРВОГО
сэмпла кадра, а не момент публикации: разница между ними и есть основа
бюджета barge-in (design §4). Собственно поэтому кадр приходится собирать
через RingBuffer с привязкой времени к каждому куску, а не конкатенацией
массивов "как есть" -- иначе штамп у составного кадра (собранного из
хвостов двух callback'ов capture) будет всегда неверным.

Про xrun. Если PortAudio сообщает input_overflow/input_underflow, кадр
не "латается нулями": состояние DC-blocker'а и ресемплера сбрасывается
(предположение о непрерывности потока нарушено), в first_sample делается
разрыв (честная оценка "как минимум ещё один кадр потерян" -- ни ALSA,
ни PortAudio не отдают точное число потерянных сэмплов на всех бэкендах),
и публикуется SystemEvent audio.xrun с воркера.
Колбэк PortAudio только копирует сырой блок в очередь: downmix/HPF/
ресемплинг/DDS на отдельном потоке. Иначе GIL+numpy каждые 16 мс
срывают следующий период ALSA. ROS-таймер исполнителя сюда не годится:
один затор DDS оставлял 18 кадров в очереди на 32 и рвал first_sample.
"""

from __future__ import annotations

import contextlib
import math
import queue
import threading
import time

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import AudioChunk, SystemEvent
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.audio_device import resolve_device
from guide_robot_voice.lib.dc_blocker import DcBlocker
from guide_robot_voice.lib.qos import QOS_AUDIO_MIC, QOS_SYSTEM_EVENT
from guide_robot_voice.lib.resampler import Resampler
from guide_robot_voice.lib.ring import RingBuffer

_SILENCE_FLOOR_DBFS = -120.0
# ~4 с кадров по 16 мс. 32 кадра (~0.5 с) переполнялись, пока исполнитель
# не успевал слить DDS. Не unbounded RAM -- при живом сливе 4 с с запасом.
_CAP_QUEUE_MAX = 256
_XRUN_LOG_SEC = 5.0


class AudioFrontendNode(LifecycleNode):
    """Lifecycle-нода захвата и первичной обработки звука."""

    def __init__(self) -> None:
        """Объявить параметры. Устройство захватывается в on_configure."""
        super().__init__("audio_frontend")

        self.declare_parameter("device", "")
        self.declare_parameter("device_rate", 48000)
        self.declare_parameter("out_rate", 16000)
        self.declare_parameter("frame_ms", 16)
        self.declare_parameter("channels_in", 1)
        self.declare_parameter("periods", 3)
        self.declare_parameter("gain_db", 0.0)
        self.declare_parameter("hpf_hz", 40.0)
        self.declare_parameter("publish_raw", False)
        self.declare_parameter("frame_id", "mic_array")
        # stage3 B5: настоящее измерение фактической частоты захвата --
        # см. on_activate(). Окно и допуск настраиваемые, дефолты из задачи.
        self.declare_parameter("rate_check_window_s", 2.0)
        self.declare_parameter("rate_check_tolerance", 0.01)
        # Stage 2+, см. design §7. Пока только объявлены и залогированы,
        # логики AEC в этой ноде нет -- добавится вместе с AEC-бэкендом.
        self.declare_parameter("aec.enabled", False)
        self.declare_parameter("aec.backend", "none")
        self.declare_parameter("aec.filter_length_ms", 200.0)

        self._stream: object | None = None
        self._dc_blocker: DcBlocker | None = None
        self._resampler: Resampler | None = None
        self._ring: RingBuffer | None = None
        self._out_frame_samples = 0
        self._gain_linear = 1.0
        self._first_sample = 0
        self._raw_first_sample = 0
        self._stage = "инициализация"
        self._level_lock = threading.Lock()
        self._level_dbfs = _SILENCE_FLOOR_DBFS
        self._xrun_count = 0
        self._published_count = 0
        self._overflows = 0
        self._overflows_total = 0
        self._xrun_log_at = 0.0
        self._pub_lock = threading.Lock()
        self._cap_queue: queue.Queue[tuple[float, np.ndarray, bool]] = queue.Queue(
            maxsize=_CAP_QUEUE_MAX
        )
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        # stage3 B5: снимаются на воркере в _process_capture(), читаются
        # из on_activate() после окна ожидания -- под одним и тем же
        # локом, поток-производитель ровно один (_worker_loop).
        self._activation_check_lock = threading.Lock()
        self._activation_validating = False
        self._activation_frames_seen = 0
        self._activation_saw_nonzero = False

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Открыть устройство захвата, поднять интерфейсы.

        Тело целиком в try, как и в tts_node: исключение из колбэка
        перехода lifecycle глушится машиной состояний без деталей причины.
        """
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался на шаге '{self._stage}': {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        """Собственно конфигурация. Каждый шаг помечается в self._stage."""
        device_rate = int(self.get_parameter("device_rate").value)
        out_rate = int(self.get_parameter("out_rate").value)
        frame_ms = int(self.get_parameter("frame_ms").value)
        channels_in = int(self.get_parameter("channels_in").value)
        periods = int(self.get_parameter("periods").value)
        gain_db = float(self.get_parameter("gain_db").value)
        hpf_hz = float(self.get_parameter("hpf_hz").value)

        self._stage = "разрешение устройства"
        device = self.get_parameter("device").value or None
        self.get_logger().info(f"открываю устройство захвата: {device or 'по умолчанию'}")
        resolved = resolve_device(device, "input", min_channels=channels_in)

        self._stage = "открытие потока"
        capture_block = int(device_rate * frame_ms / 1000)
        latency = periods * frame_ms / 1000.0
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=device_rate,
            channels=channels_in,
            dtype="int16",
            blocksize=capture_block,
            latency=latency,
            device=resolved,
            callback=self._callback,
        )

        # Настоящая проверка фактической частоты -- в on_activate(), по
        # живым сэмплам за первые rate_check_window_s секунд захвата
        # (design §3.1). `self._stream.samplerate` здесь -- всего лишь эхо
        # ЗАПРОШЕННОЙ частоты от PortAudio, не измерение: сравнивать
        # запрошенное с самим собой тавтологично и ничего не ловит --
        # именно так исторически прошёл мимо случай со скрытым
        # ресемплингом plughw:/pulse.

        self._stage = "цепочка обработки"
        self._dc_blocker = DcBlocker(device_rate, cutoff_hz=hpf_hz)
        self._resampler = Resampler(device_rate, out_rate)
        self._ring = RingBuffer(out_rate)
        self._out_frame_samples = int(out_rate * frame_ms / 1000)
        self._gain_linear = 10.0 ** (gain_db / 20.0) if gain_db != 0.0 else 1.0
        if not self._resampler.passthrough:
            engine = "scipy polyphase" if self._resampler.uses_scipy else "линейная интерполяция"
            self.get_logger().info(f"ресемплинг {device_rate} -> {out_rate} Гц ({engine})")

        self._stage = "интерфейсы ROS"
        self._mic_pub = self.create_lifecycle_publisher(AudioChunk, "/audio/mic", QOS_AUDIO_MIC)
        self._raw_pub = None
        if bool(self.get_parameter("publish_raw").value):
            self._raw_pub = self.create_lifecycle_publisher(
                AudioChunk, "/audio/mic_raw", QOS_AUDIO_MIC
            )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)
        self._start_worker()

        if bool(self.get_parameter("aec.enabled").value):
            self.get_logger().warning(
                "aec.enabled=true, но AEC ещё не реализован (Stage 2+, design §7) -- "
                "параметр проигнорирован"
            )

        self._stage = "готово"
        self.get_logger().info(
            f"audio_frontend сконфигурирован: запрошено {device_rate} Гц "
            f"(фактическая частота проверяется на activate), "
            f"выход {out_rate} Гц, кадр {frame_ms} мс ({self._out_frame_samples} сэмплов)"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Запустить поток захвата и проверить фактическую частоту (stage3 B5).

        До этого момента `_stream.samplerate` -- лишь эхо запроса, не
        измерение (см. `_configure()`). Настоящая проверка требует РЕАЛЬНЫХ
        сэмплов: копим кол-во кадров и факт ненулевого сигнала на воркере
        (`_process_capture()`) за `rate_check_window_s`, здесь просто ждём
        окно и читаем накопленное. >tolerance расхождения с device_rate или
        полная тишина (весь буфер -- нули, живой инцидент "фронтенд поднял
        не тот оверлей и молчал") -- отказ активации, поток останавливается.
        """
        try:
            assert self._stream is not None
            self._first_sample = 0
            self._raw_first_sample = 0
            with self._activation_check_lock:
                self._activation_validating = True
                self._activation_frames_seen = 0
                self._activation_saw_nonzero = False
            self._stream.start()  # type: ignore[attr-defined]
            started_at = time.monotonic()
            time.sleep(float(self.get_parameter("rate_check_window_s").value))
            error_detail = self._check_actual_capture_rate(time.monotonic() - started_at)
            if error_detail is not None:
                self.get_logger().error(f"{error_detail} Активация отклонена.")
                self._stream.stop()  # type: ignore[attr-defined]
                return TransitionCallbackReturn.FAILURE
        except Exception as error:
            self.get_logger().error(f"activate не удался: {error}")
            return TransitionCallbackReturn.FAILURE
        return super().on_activate(state)

    def _check_actual_capture_rate(self, elapsed_s: float) -> str | None:
        """Сравнить измеренную частоту/тишину с ожидаемым. None -- всё в порядке."""
        with self._activation_check_lock:
            self._activation_validating = False
            frames_seen = self._activation_frames_seen
            saw_nonzero = self._activation_saw_nonzero

        window_s = float(self.get_parameter("rate_check_window_s").value)
        if not saw_nonzero:
            return (
                f"все сэмплы за первые {window_s:.1f}с активации -- тишина (нули): "
                "устройство/оверлей, вероятно, не тот."
            )

        device_rate = int(self.get_parameter("device_rate").value)
        actual_rate = frames_seen / elapsed_s if elapsed_s > 0 else 0.0
        tolerance = float(self.get_parameter("rate_check_tolerance").value)
        mismatch = abs(actual_rate - device_rate) / device_rate if device_rate else 1.0
        if mismatch > tolerance:
            return (
                f"измеренная частота захвата {actual_rate:.0f} Гц расходится с заявленными "
                f"{device_rate} Гц больше чем на {tolerance:.0%} ({mismatch:.1%}): "
                "PortAudio, похоже, подставил скрытый ресемплинг."
            )

        self.get_logger().info(
            f"фактическая частота захвата подтверждена: {actual_rate:.0f} Гц "
            f"(заявлено {device_rate} Гц, окно {window_s:.1f}с)"
        )
        return None

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Остановить поток. Устройство остаётся открытым (design §3.1)."""
        if self._stream is not None:
            self._stream.stop()  # type: ignore[attr-defined]
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Закрыть устройство."""
        del state
        if self._stream is not None:
            try:
                self._stream.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._stop_worker()
        if self._stream is not None:
            self._stream.close()  # type: ignore[attr-defined]
            self._stream = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- захват -----------------------------------------------------------

    def _callback(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        """Колбэк PortAudio. Только memcpy в очередь -- обработка на воркере."""
        del time_info
        try:
            now = self.get_clock().now().nanoseconds / 1e9
            capture_time = now - frames / float(self._stream.samplerate)  # type: ignore[attr-defined]
            xrun = bool(getattr(status, "input_overflow", False)) or bool(
                getattr(status, "input_underflow", False)
            )
            self._cap_queue.put_nowait((capture_time, indata.copy(), xrun))
        except queue.Full:
            with self._pub_lock:
                self._overflows += 1
                self._overflows_total += 1
        except Exception:
            # Лог из RT-потока сам провоцирует следующие xrun.
            with self._pub_lock:
                self._overflows += 1
                self._overflows_total += 1

    def _process_capture(self, capture_time: float, indata: np.ndarray, xrun: bool) -> None:
        """Downmix / HPF / ресемплинг. Живёт на воркере, не в ALSA и не на таймере."""
        self._record_activation_check_sample(indata)
        if xrun:
            self._handle_xrun()

        mono = self._downmix(indata)
        if self._raw_pub is not None:
            self._publish_raw_if_enabled(capture_time, mono)

        assert self._dc_blocker is not None
        assert self._resampler is not None
        assert self._ring is not None
        filtered = self._dc_blocker.process(mono)
        if self._gain_linear != 1.0:
            filtered = np.clip(
                filtered.astype(np.float64) * self._gain_linear, -32768, 32767
            ).astype(np.int16)
        self._update_level(filtered)

        converted = self._resampler.process(filtered)
        if converted.size:
            self._ring.push(capture_time, converted)
        self._drain_ring()

    def _record_activation_check_sample(self, indata: np.ndarray) -> None:
        """Копить кадры/факт ненулевого сигнала для проверки в on_activate() (stage3 B5)."""
        with self._activation_check_lock:
            if not self._activation_validating:
                return
            self._activation_frames_seen += indata.shape[0]
            if not self._activation_saw_nonzero and bool(np.any(indata != 0)):
                self._activation_saw_nonzero = True

    def _downmix(self, indata: np.ndarray) -> np.ndarray:
        """Свести к моно. На Stage 1 тривиально -- один канал или среднее."""
        if indata.ndim == 1 or indata.shape[1] == 1:
            return indata.reshape(-1)
        return indata.mean(axis=1).astype(np.int16)

    def _handle_xrun(self) -> None:
        """Сбросить фильтры. Разрыв first_sample и лог -- на воркере.

        Ни ALSA, ни PortAudio не отдают точное число потерянных сэмплов
        на всех бэкендах, поэтому first_sample сдвигается на один кадр
        сверх обычного шага -- это лишь гарантирует ОБНАРУЖИМОСТЬ разрыва
        потребителем (design: "разрыв в first_sample", не точная величина).
        """
        self._xrun_count += 1
        if self._dc_blocker is not None:
            self._dc_blocker.reset()
        if self._resampler is not None:
            self._resampler.reset()
        self._first_sample += self._out_frame_samples
        if self._log_xrun_throttled("input overflow"):
            event = SystemEvent(
                id="audio.xrun",
                severity=SystemEvent.ERROR,
                detail=f"count={self._xrun_count}",
            )
            event.header.stamp = self.get_clock().now().to_msg()
            self._event_pub.publish(event)

    def _drain_ring(self) -> None:
        """Выдать все полностью накопленные кадры фиксированной длины."""
        assert self._ring is not None
        while True:
            popped = self._ring.pop_exact(self._out_frame_samples)
            if popped is None:
                return
            timestamp, frame = popped
            self._publish_frame(timestamp, frame)

    def _start_worker(self) -> None:
        """Поток: pop очереди захвата -> обработка -> publish. Не таймер исполнителя."""
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="audio_frontend_pub", daemon=True
        )
        self._worker.start()

    def _stop_worker(self) -> None:
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    def _worker_loop(self) -> None:
        """Слить cap_queue полностью, кадр за кадром, без ожидания тика ROS."""
        while not self._worker_stop.is_set():
            try:
                capture_time, indata, xrun = self._cap_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._pub_lock:
                dropped = self._overflows
                self._overflows = 0
            if dropped:
                self._first_sample += dropped * self._out_frame_samples
                self._log_xrun_throttled(f"cap_queue overflow x{dropped}")
            try:
                self._process_capture(capture_time, indata, xrun)
            except Exception as error:
                self.get_logger().error(f"сбой обработки кадра захвата: {error}")

    def _log_xrun_throttled(self, detail: str) -> bool:
        """ERROR раз в 5 с: сам лог/DDS на xrun жрёт CPU. True -- лог ушёл."""
        now = time.monotonic()
        if now - self._xrun_log_at < _XRUN_LOG_SEC:
            return False
        self._xrun_log_at = now
        self.get_logger().error(f"audio.xrun: {detail}, count={self._xrun_count}")
        return True

    def _publish_frame(self, timestamp: float, frame: np.ndarray) -> None:
        """Опубликовать один кадр /audio/mic."""
        msg = AudioChunk()
        msg.header.stamp = self._seconds_to_time_msg(timestamp)
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.sample_rate = int(self.get_parameter("out_rate").value)
        msg.channels = 1
        msg.data = frame.tolist()
        msg.first_sample = self._first_sample
        self._mic_pub.publish(msg)
        self._first_sample += frame.shape[0]
        self._published_count += 1

    def _publish_raw_if_enabled(self, capture_time: float, mono: np.ndarray) -> None:
        """Диагностический дубль на device_rate, без фильтрации/ресемплинга."""
        if self._raw_pub is None:
            return
        msg = AudioChunk()
        msg.header.stamp = self._seconds_to_time_msg(capture_time)
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.sample_rate = int(self.get_parameter("device_rate").value)
        msg.channels = 1
        msg.data = mono.tolist()
        msg.first_sample = self._raw_first_sample
        self._raw_pub.publish(msg)
        self._raw_first_sample += mono.shape[0]

    def _update_level(self, pcm: np.ndarray) -> None:
        """Обновить оценку уровня сигнала для /diagnostics."""
        if pcm.size == 0:
            return
        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
        dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0.0 else _SILENCE_FLOOR_DBFS
        with self._level_lock:
            self._level_dbfs = max(dbfs, _SILENCE_FLOOR_DBFS)

    def _seconds_to_time_msg(self, seconds: float) -> object:
        """Перевести время в секундах (time.time()-подобное) в builtin_interfaces/Time."""
        from builtin_interfaces.msg import Time as TimeMsg

        sec = int(seconds)
        nanosec = round((seconds - sec) * 1e9)
        return TimeMsg(sec=sec, nanosec=nanosec)

    # -- диагностика ------------------------------------------------------

    def _publish_diagnostics(self) -> None:
        """Раз в секунду опубликовать уровень сигнала и счётчики (design §6, шаг 4)."""
        with self._level_lock:
            level = self._level_dbfs
        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()
        warn = bool(self._xrun_count or self._overflows_total)
        entry = DiagnosticStatus(
            name="voice/audio_frontend",
            hardware_id="audio_frontend",
            level=DiagnosticStatus.WARN if warn else DiagnosticStatus.OK,
            message=f"level_dbfs={level:.1f}",
            values=[
                KeyValue(key="level_dbfs", value=f"{level:.1f}"),
                KeyValue(key="xrun_count", value=str(self._xrun_count)),
                KeyValue(key="queue_overflows", value=str(self._overflows_total)),
                KeyValue(key="first_sample", value=str(self._first_sample)),
                KeyValue(key="published_frames", value=str(self._published_count)),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = AudioFrontendNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
