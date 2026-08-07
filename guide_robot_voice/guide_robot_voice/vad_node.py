"""Нода детекции речи (VAD).

Собирает вместе два независимо тестируемых куска из lib/: инференс
Silero VAD v5 и гистерезис активности. Сама нода отвечает только за
ROS-обвязку -- накопление кадров /audio/mic (256 сэмплов @ 16 кГц) в окна
ровно по 512 сэмплов, которых требует модель (design §3.1: кадр захвата
подобран так, чтобы 512 = 2 кадра), и публикацию VoiceActivity.

Barge-in: при входе в речь, если TTS сейчас говорит (/voice/speaking,
не протухший) и barge_in_enabled -- публикует CancelAll(scope=SCOPE_ALL,
reason=REASON_BARGE_IN). Живёт здесь, а не в mission/LLM, сознательно:
это L1-путь, обязан работать при мёртвом LLM. Задержка = один хоп DDS
(design §3.2).

barge_in_min_windows -- НЕЗАВИСИМОЕ от enter_windows подтверждение:
считает подряд идущие окна с вероятностью выше enter_threshold сам по
себе, не через гистерезис. Разделены сознательно: базовый /vad -- это
телеметрия, а CancelAll -- широковещательная команда безопасности,
и для неё оправдан отдельный (в общем случае более консервативный)
порог подтверждения, не завязанный на то, каким публикуется /vad.

require_aec_for_barge_in: AEC ещё не существует (Stage 2+, design §7),
поэтому "AEC активен" здесь всегда False. Если параметр включён, barge-in
выключен целиком -- это и есть защита от сценария "переехали на железо,
забыли включить AEC, робот перебивает сам себя", реализованная максимально
консервативно: нет источника подтверждения AEC -- нет и barge-in.

Про xrun. audio_frontend уже сбрасывает свои фильтры при разрыве потока,
но разрыв в first_sample -- это разрыв и для RNN-состояния Silero, и для
гистерезиса: то, что "было до дыры", не должно склеиваться с тем, что
после. Поэтому vad_node сам проверяет first_sample на непрерывность и
сбрасывается независимо, а не полагается на то, что источник восстановится
"как ни в чём не бывало".
"""

from __future__ import annotations

import math
import threading

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import AudioChunk, CancelAll, SpeakingStatus, VoiceActivity
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.qos import (
    QOS_AUDIO_MIC,
    QOS_CANCEL_ALL,
    QOS_VAD,
    QOS_VOICE_SPEAKING,
)
from guide_robot_voice.lib.ring import RingBuffer
from guide_robot_voice.lib.vad_hysteresis import VadHysteresis
from guide_robot_voice.lib.vad_model import SileroVad

_WINDOW_SAMPLES = 512
_SILENCE_FLOOR_DBFS = -120.0
_SPEAKING_STATUS_STALE_SEC = 0.4
"""Два периода heartbeat (design §2): протухший статус считается speaking=false."""


class VadNode(LifecycleNode):
    """Lifecycle-нода детекции речи."""

    def __init__(self) -> None:
        """Объявить параметры. Модель загружается в on_configure."""
        super().__init__("vad_node")

        self.declare_parameter("model_path", "")
        self.declare_parameter("enter_threshold", 0.65)
        self.declare_parameter("exit_threshold", 0.35)
        self.declare_parameter("enter_windows", 2)
        self.declare_parameter("hangover_ms", 400.0)
        self.declare_parameter("min_speech_ms", 120.0)
        self.declare_parameter("frame_id", "mic_array")
        self.declare_parameter("barge_in_enabled", True)
        self.declare_parameter("barge_in_min_windows", 2)
        self.declare_parameter("require_aec_for_barge_in", False)

        self._vad: SileroVad | None = None
        self._hysteresis: VadHysteresis | None = None
        self._ring: RingBuffer | None = None
        self._is_active = False

        self._lock = threading.Lock()
        self._expected_first_sample: int | None = None
        self._activations_total = 0
        self._short_segments_total = 0
        self._last_probability = 0.0
        self._last_level_dbfs = _SILENCE_FLOOR_DBFS
        self._was_active = False

        self._latest_speaking: SpeakingStatus | None = None
        self._barge_in_streak = 0
        self._barge_in_armed = True
        """False сразу после срабатывания -- одно барж-ин на сегмент речи."""
        self._barge_in_triggers_total = 0

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить модель, поднять интерфейсы."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        model_path = str(self.get_parameter("model_path").value)
        if not model_path:
            raise ValueError(
                "параметр model_path не задан. Модель лежит в репозитории "
                "(models/silero_vad.onnx, git-lfs), см. config/voice.yaml"
            )

        self.get_logger().info("загружаю модель VAD...")
        self._vad = SileroVad(model_path)
        self._vad.load()

        self._hysteresis = VadHysteresis(
            enter_threshold=float(self.get_parameter("enter_threshold").value),
            exit_threshold=float(self.get_parameter("exit_threshold").value),
            enter_windows=int(self.get_parameter("enter_windows").value),
            hangover_ms=float(self.get_parameter("hangover_ms").value),
            min_speech_ms=float(self.get_parameter("min_speech_ms").value),
            window_ms=1000.0 * _WINDOW_SAMPLES / 16000.0,
        )
        self._ring = RingBuffer(16000)

        self._vad_pub = self.create_lifecycle_publisher(VoiceActivity, "/vad", QOS_VAD)
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._cancel_pub = self.create_lifecycle_publisher(
            CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL
        )
        self._mic_sub = self.create_subscription(
            AudioChunk, "/audio/mic", self._on_audio, QOS_AUDIO_MIC
        )
        self._speaking_sub = self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speaking_status, QOS_VOICE_SPEAKING
        )
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)

        self.get_logger().info("vad_node сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Сбросить состояние и начать обработку входящих кадров."""
        assert self._vad is not None
        assert self._hysteresis is not None
        assert self._ring is not None
        self._vad.reset()
        self._hysteresis.reset()
        self._ring = RingBuffer(16000)
        self._expected_first_sample = None
        self._was_active = False
        self._barge_in_streak = 0
        self._barge_in_armed = True
        with self._lock:
            self._is_active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Перестать обрабатывать входящие кадры."""
        with self._lock:
            self._is_active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить модель."""
        del state
        if self._vad is not None:
            self._vad.close()
            self._vad = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- обработка --------------------------------------------------------

    def _on_audio(self, msg: AudioChunk) -> None:
        """Накопить кадр в окна по 512 сэмплов и прогнать через VAD."""
        with self._lock:
            if not self._is_active:
                return

        expected = self._expected_first_sample
        if expected is not None and msg.first_sample != expected:
            self.get_logger().warning(
                f"разрыв в /audio/mic: ожидался first_sample={expected}, "
                f"пришёл {msg.first_sample} -- сбрасываю состояние"
            )
            assert self._vad is not None
            assert self._hysteresis is not None
            self._vad.reset()
            self._hysteresis.reset()
            self._ring = RingBuffer(16000)
        self._expected_first_sample = msg.first_sample + len(msg.data)

        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        samples = np.array(msg.data, dtype=np.int16)

        assert self._ring is not None
        self._ring.push(timestamp, samples)
        self._drain_ring()

    def _drain_ring(self) -> None:
        assert self._ring is not None
        while True:
            popped = self._ring.pop_exact(_WINDOW_SAMPLES)
            if popped is None:
                return
            window_timestamp, window = popped
            self._process_window(window_timestamp, window)

    def _process_window(self, timestamp: float, window: np.ndarray) -> None:
        assert self._vad is not None
        assert self._hysteresis is not None

        probability = self._vad.process(window)
        result = self._hysteresis.update(probability)
        level_dbfs = self._level_dbfs(window)

        self._last_probability = probability
        self._last_level_dbfs = level_dbfs
        if result.active and not self._was_active:
            self._activations_total += 1
        self._was_active = result.active
        if result.segment_ended_too_short:
            self._short_segments_total += 1
        if not result.active:
            # Сегмент речи закончился (или ещё не начинался) -- взвести
            # барж-ин заново для следующего.
            self._barge_in_armed = True

        self._maybe_trigger_barge_in(timestamp, probability)

        msg = VoiceActivity()
        msg.header.stamp = self._seconds_to_time_msg(timestamp)
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.active = result.active
        msg.probability = probability
        msg.state_duration = result.state_duration
        msg.level_dbfs = level_dbfs
        self._vad_pub.publish(msg)

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        """Запомнить последний статус TTS. Критический путь -- держать коротким."""
        self._latest_speaking = msg

    def _is_tts_speaking(self) -> bool:
        """TTS сейчас говорит, и статус не протух (design §2, 400 мс)."""
        status = self._latest_speaking
        if status is None or not status.speaking:
            return False
        stamp = status.stamp.sec + status.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        return age <= _SPEAKING_STATUS_STALE_SEC

    def _maybe_trigger_barge_in(self, window_timestamp: float, probability: float) -> None:
        """Независимое от гистерезиса подтверждение входа в речь для CancelAll."""
        enter_threshold = float(self.get_parameter("enter_threshold").value)
        if probability > enter_threshold:
            self._barge_in_streak += 1
        else:
            self._barge_in_streak = 0

        if not self._barge_in_armed:
            return
        if self._barge_in_streak < int(self.get_parameter("barge_in_min_windows").value):
            return
        if not bool(self.get_parameter("barge_in_enabled").value):
            return
        if bool(self.get_parameter("require_aec_for_barge_in").value):
            # AEC не существует (Stage 2+) -- нет источника подтверждения,
            # значит подтверждения нет, значит barge-in не срабатывает.
            return
        if not self._is_tts_speaking():
            return

        self._barge_in_armed = False
        self._barge_in_triggers_total += 1
        self._publish_cancel_all(window_timestamp)

    def _publish_cancel_all(self, onset_timestamp: float) -> None:
        """Опубликовать аварийную отмену.

        onset_timestamp -- момент начала речи, не момент публикации: разница
        между ними и есть часть бюджета barge-in (design §4), и её нужно
        передать дальше для измерения latency в tts_node.
        """
        msg = CancelAll()
        msg.stamp = self._seconds_to_time_msg(onset_timestamp)
        msg.epoch = self.get_clock().now().nanoseconds
        msg.scope = CancelAll.SCOPE_ALL
        msg.reason = CancelAll.REASON_BARGE_IN
        self._cancel_pub.publish(msg)
        self.get_logger().info("barge-in: посетитель заговорил во время речи робота")

    def _level_dbfs(self, pcm: np.ndarray) -> float:
        if pcm.size == 0:
            return _SILENCE_FLOOR_DBFS
        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
        if rms <= 0.0:
            return _SILENCE_FLOOR_DBFS
        return max(20.0 * math.log10(rms / 32768.0), _SILENCE_FLOOR_DBFS)

    def _seconds_to_time_msg(self, seconds: float) -> object:
        from builtin_interfaces.msg import Time as TimeMsg

        sec = int(seconds)
        nanosec = round((seconds - sec) * 1e9)
        return TimeMsg(sec=sec, nanosec=nanosec)

    # -- диагностика ------------------------------------------------------

    def _publish_diagnostics(self) -> None:
        """Раз в секунду -- счётчики для проверки false-wake rate (design §6, шаг 5)."""
        active = self._hysteresis.active if self._hysteresis is not None else False
        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()
        entry = DiagnosticStatus(
            name="voice/vad",
            hardware_id="vad_node",
            level=DiagnosticStatus.OK,
            message="active" if active else "idle",
            values=[
                KeyValue(key="probability", value=f"{self._last_probability:.3f}"),
                KeyValue(key="level_dbfs", value=f"{self._last_level_dbfs:.1f}"),
                KeyValue(key="activations_total", value=str(self._activations_total)),
                KeyValue(key="short_segments_total", value=str(self._short_segments_total)),
                KeyValue(key="barge_in_triggers_total", value=str(self._barge_in_triggers_total)),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = VadNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
