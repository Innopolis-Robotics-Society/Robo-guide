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
и немедленно публикуется SystemEvent(severity=ERROR, id="audio.xrun").
"""

from __future__ import annotations

import math
import threading

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

        self._stage = "проверка фактической частоты"
        actual_rate = round(self._stream.samplerate)  # type: ignore[attr-defined]
        if actual_rate != device_rate:
            # PortAudio молча подставляет ресемплинг, если устройство не
            # поддерживает запрошенную частоту напрямую -- на этом пути
            # нет никакого контроля над фильтром, и тайминги перестают
            # быть тем, что заявлено в design §3.1.
            raise ValueError(
                f"устройство приняло {actual_rate} Гц вместо запрошенных {device_rate} Гц: "
                "похоже, PortAudio подставил скрытый ресемплинг. Проверьте, что hw: "
                "открывается напрямую, а не через plughw:/pulse."
            )

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

        if bool(self.get_parameter("aec.enabled").value):
            self.get_logger().warning(
                "aec.enabled=true, но AEC ещё не реализован (Stage 2+, design §7) -- "
                "параметр проигнорирован"
            )

        self._stage = "готово"
        self.get_logger().info(
            f"audio_frontend сконфигурирован: устройство {actual_rate} Гц, "
            f"выход {out_rate} Гц, кадр {frame_ms} мс ({self._out_frame_samples} сэмплов)"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Запустить поток захвата."""
        try:
            assert self._stream is not None
            self._first_sample = 0
            self._raw_first_sample = 0
            self._stream.start()  # type: ignore[attr-defined]
        except Exception as error:
            self.get_logger().error(f"activate не удался: {error}")
            return TransitionCallbackReturn.FAILURE
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Остановить поток. Устройство остаётся открытым (design §3.1)."""
        if self._stream is not None:
            self._stream.stop()  # type: ignore[attr-defined]
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Закрыть устройство."""
        del state
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
        """Колбэк PortAudio. Вызывается из отдельного аудио-потока.

        Публикация rclpy из чужого потока безопасна (Publisher.publish()
        не привязан к executor'у) -- отдельного механизма передачи в ROS-
        поток не заводим, это лишняя сущность без выигрыша (design и так
        не требует единого потока для ноды).
        """
        del time_info
        try:
            now = self.get_clock().now().nanoseconds / 1e9
            capture_time = now - frames / float(self._stream.samplerate)  # type: ignore[attr-defined]

            xrun = bool(getattr(status, "input_overflow", False)) or bool(
                getattr(status, "input_underflow", False)
            )
            if xrun:
                self._handle_xrun(status)

            mono = self._downmix(indata)
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
        except Exception as error:
            self.get_logger().error(f"сбой в колбэке захвата: {error}")

    def _downmix(self, indata: np.ndarray) -> np.ndarray:
        """Свести к моно. На Stage 1 тривиально -- один канал или среднее."""
        if indata.ndim == 1 or indata.shape[1] == 1:
            return indata.reshape(-1)
        return indata.mean(axis=1).astype(np.int16)

    def _handle_xrun(self, status: object) -> None:
        """Сбросить состояние непрерывности и сообщить о разрыве честно.

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
        overflow = getattr(status, "input_overflow", None)
        detail = f"input_overflow={overflow}, count={self._xrun_count}"
        self.get_logger().error(f"audio.xrun: {detail}")
        event = SystemEvent(id="audio.xrun", severity=SystemEvent.ERROR, detail=detail)
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
        entry = DiagnosticStatus(
            name="voice/audio_frontend",
            hardware_id="audio_frontend",
            level=DiagnosticStatus.WARN if self._xrun_count else DiagnosticStatus.OK,
            message=f"level_dbfs={level:.1f}",
            values=[
                KeyValue(key="level_dbfs", value=f"{level:.1f}"),
                KeyValue(key="xrun_count", value=str(self._xrun_count)),
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
