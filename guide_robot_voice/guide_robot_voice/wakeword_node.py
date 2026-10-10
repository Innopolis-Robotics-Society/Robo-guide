"""Нода детекции ключевого слова (wakeword / стоп-слово).

Два бэкенда за одним параметром backend (design §3.3):
  asr_kws (Stage 1, реализован здесь) -- подписка на /asr/partial,
    нечёткое сравнение по Левенштейну (lib/keyword_spotter.py). Даёт
    стоп-слово бесплатно, ценой задержки ASR (~300-500 мс) и зависимости
    от работающего asr_node.
  oww -- локальная frozen ONNX-модель поверх окон 1280 сэмплов /audio/mic.
    Публикует только /voice/wakeword_candidate; принятый /speech/wakeword,
    admission и guarded CancelAll принадлежат voice_session_manager.

Стоп-слова -- L1-путь, как и barge-in в vad_node: нода сама публикует
CancelAll(scope=SCOPE_ALL, reason=REASON_WAKEWORD), не дожидаясь mission.
В legacy asr_kws активационные фразы («Фирая») тоже CancelAll. В oww
имя в ASR-партиале игнорируется: это не новое акустическое обращение.

Во время TTS партиалы всё ещё обрабатываются (asr: wakeword_listen_during_tts):
без этого gate_on_tts глушит ASR и «Фирая» не слышна. Под TTS требуем
почти точное совпадение (confidence ≥ 0.99) и совпадение с НАЧАЛА фразы:
иначе эхо «я робот-экскурсовод» даёт CancelAll с confidence=1.0.

Имя робота -- Фирая -- редкое, GigaAM пишет его по-разному («фирайя»,
«фира я», «фи рая»). Варианты перечислены в activation_phrases явно:
вне TTS их и так ловит Левенштейн, но под TTS нужен точный матч
(confidence ≥ 0.99), и там побеждает только буквальное написание.

tts_active берётся из последнего /voice/speaking. В отличие от vad_node
и asr_node, протухший статус здесь не просто тихо считается false --
design §3.3 явно требует WARN в диагностику: без tts_active это поле
Wakeword.msg превращается во враньё, а именно по нему считается
false-wake-under-TTS -- приёмочная метрика пакета.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import (
    AudioChunk,
    CancelAll,
    SpeakingStatus,
    Transcript,
    Wakeword,
    WakewordCandidate,
)
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.keyword_spotter import KeywordSpotter
from guide_robot_voice.lib.qos import (
    QOS_ASR_PARTIAL,
    QOS_ASR_TRANSCRIPT,
    QOS_AUDIO_MIC,
    QOS_CANCEL_ALL,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_voice.lib.wakeword_runtime import RATE, FrozenWakewordModel, WakewordStream

_SPEAKING_STATUS_STALE_SEC = 0.4


class WakewordNode(LifecycleNode):
    """Lifecycle-нода детекции ключевого слова."""

    def __init__(self) -> None:
        """Объявить параметры. Споттеры собираются в on_configure."""
        super().__init__("wakeword_node")

        self.declare_parameter("backend", "asr_kws")
        self.declare_parameter(
            "activation_phrases",
            ["фирая", "фирайя", "фира я", "фи рая", "эй фирая", "слушай фирая"],
        )
        self.declare_parameter("stop_phrases", ["стоп", "стой", "хватит", "замолчи"])
        self.declare_parameter("fuzzy_max_distance", 1)
        self.declare_parameter("min_confidence", 0.5)
        self.declare_parameter("refractory_ms", 1500.0)
        self.declare_parameter("frame_id", "mic_array")
        self.declare_parameter("model_bundle_path", "")

        self._activation_spotter: KeywordSpotter | None = None
        self._stop_spotter: KeywordSpotter | None = None
        self._is_active = False
        self._lock = threading.Lock()

        self._latest_speaking: SpeakingStatus | None = None
        self._last_trigger_at: dict[str, float] = {}
        self._last_trigger_utterance: dict[str, int] = {}
        self._triggers_total = 0
        self._stale_speaking_warnings = 0
        self._backend = "asr_kws"
        self._oww_stream: WakewordStream | None = None
        self._model_sha256 = ""
        self._runtime_error = ""

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Собрать споттеры, поднять интерфейсы."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        backend = str(self.get_parameter("backend").value)
        if backend not in ("asr_kws", "oww"):
            raise ValueError(f"неизвестный backend: {backend!r}, ожидается asr_kws или oww")
        if backend == "oww":
            bundle = str(self.get_parameter("model_bundle_path").value)
            if not bundle:
                raise ValueError("backend=oww требует model_bundle_path")
            model = FrozenWakewordModel(Path(bundle))
            self._oww_stream = WakewordStream(model, model.manifest["inference_config"])
            self._model_sha256 = model.manifest["sha256"]["firaya_trial.onnx"]
        self._backend = backend
        self._runtime_error = ""

        max_distance = int(self.get_parameter("fuzzy_max_distance").value)
        activation_phrases = list(self.get_parameter("activation_phrases").value)
        stop_phrases = list(self.get_parameter("stop_phrases").value)
        self._activation_spotter = KeywordSpotter(activation_phrases, max_distance)
        self._stop_spotter = KeywordSpotter(stop_phrases, max_distance)

        self._wakeword_pub = self.create_lifecycle_publisher(
            Wakeword, "/speech/wakeword", QOS_WAKEWORD
        )
        self._cancel_pub = self.create_lifecycle_publisher(
            CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        if backend == "oww":
            self._candidate_pub = self.create_lifecycle_publisher(
                WakewordCandidate, "/voice/wakeword_candidate", QOS_WAKEWORD
            )
            self._mic_sub = self.create_subscription(
                AudioChunk, "/audio/mic", self._on_audio, QOS_AUDIO_MIC
            )
        self._partial_sub = self.create_subscription(
            Transcript, "/asr/partial", self._on_partial, QOS_ASR_PARTIAL
        )
        self._transcript_sub = self.create_subscription(
            Transcript, "/asr/transcript", self._on_partial, QOS_ASR_TRANSCRIPT
        )
        self._speaking_sub = self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speaking_status, QOS_VOICE_SPEAKING
        )
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)

        self.get_logger().info(
            f"wakeword_node сконфигурирован: backend={backend}, "
            f"активация={activation_phrases}, стоп={stop_phrases}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Сбросить рефрактерное состояние и начать обработку."""
        self._last_trigger_at = {}
        self._last_trigger_utterance = {}
        self._latest_speaking = None
        with self._lock:
            if self._oww_stream is not None:
                self._oww_stream.reset()
            self._is_active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Перестать обрабатывать входящие партиалы."""
        with self._lock:
            self._is_active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить споттеры."""
        del state
        with self._lock:
            self._is_active = False
            self._oww_stream = None
            self._activation_spotter = None
            self._stop_spotter = None
        for name in ("_partial_sub", "_transcript_sub", "_speaking_sub", "_mic_sub"):
            entity = getattr(self, name, None)
            if entity is not None:
                self.destroy_subscription(entity)
                setattr(self, name, None)
        timer = getattr(self, "_diag_timer", None)
        if timer is not None:
            self.destroy_timer(timer)
            self._diag_timer = None
        for name in ("_wakeword_pub", "_cancel_pub", "_candidate_pub", "_diag_pub"):
            entity = getattr(self, name, None)
            if entity is not None:
                self.destroy_lifecycle_publisher(entity)
                setattr(self, name, None)
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- вход -----------------------------------------------------------

    def _on_audio(self, msg: AudioChunk) -> None:
        """Акустический кандидат НЕ открывает диалог и НЕ отменяет TTS сам."""
        with self._lock:
            if not self._is_active or self._oww_stream is None:
                return
            try:
                if msg.sample_rate != RATE or msg.channels != 1:
                    raise ValueError("KWS требует processed mono PCM16 @ 16000 Hz")
                timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
                detections = self._oww_stream.push(
                    msg.device_session_id,
                    int(msg.first_sample),
                    timestamp,
                    np.asarray(msg.data, dtype=np.int16),
                )
                self._runtime_error = ""
                for detection in detections:
                    candidate = WakewordCandidate()
                    nanoseconds = round(detection.timestamp * 1e9)
                    candidate.header.stamp.sec = nanoseconds // 1_000_000_000
                    candidate.header.stamp.nanosec = nanoseconds % 1_000_000_000
                    candidate.header.frame_id = str(self.get_parameter("frame_id").value)
                    candidate.device_session_id = detection.device_session_id
                    candidate.end_sample = detection.end_sample
                    candidate.keyword = "фирая"
                    candidate.confidence = detection.confidence
                    candidate.model_sha256 = self._model_sha256
                    self._candidate_pub.publish(candidate)
                    self._triggers_total += 1
                    self.get_logger().info(
                        f"KWS candidate #{self._triggers_total}: ФИРАЯ "
                        f"score={detection.confidence:.3f} "
                        f"end_sample={detection.end_sample}"
                    )
            except Exception as error:
                message = str(error)
                if message != self._runtime_error:
                    self.get_logger().error(f"KWS fail-closed: {message}")
                self._runtime_error = message
                self._oww_stream.reset()

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        self._latest_speaking = msg

    def _speaking_now(self) -> bool:
        """Свежий heartbeat /voice/speaking. Без WARN -- горячий путь партиалов."""
        status = self._latest_speaking
        if status is None or not status.speaking:
            return False
        stamp = status.stamp.sec + status.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        return age <= _SPEAKING_STATUS_STALE_SEC

    def _tts_active(self) -> bool:
        """tts_active для Wakeword.msg. Протухший статус -- false, но с WARN.

        В отличие от vad_node/asr_node, протухание здесь не тихо: без
        честного tts_active метрика false-wake-under-TTS (design §8)
        считается неверно, а не просто "чуть более осторожно".
        """
        status = self._latest_speaking
        if status is None:
            return False
        stamp = status.stamp.sec + status.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        if age > _SPEAKING_STATUS_STALE_SEC:
            self._stale_speaking_warnings += 1
            self.get_logger().warning(
                f"/voice/speaking протух ({age * 1000:.0f} мс) -- tts_active=false "
                "не гарантированно верно"
            )
            return False
        return status.speaking

    def _on_partial(self, msg: Transcript) -> None:
        with self._lock:
            if not self._is_active:
                return
        assert self._activation_spotter is not None
        assert self._stop_spotter is not None

        speaking = self._speaking_now()
        min_confidence = float(self.get_parameter("min_confidence").value)
        # Под TTS эхо колонки в ASR -- только точное совпадение и только с
        # начала фразы: иначе «привет, я робот-экскурсовод» рвёт сам себя.
        if speaking:
            min_confidence = max(min_confidence, 0.99)

        stop_match = self._stop_spotter.find(msg.text, leading=speaking)
        if stop_match is not None and stop_match.confidence >= min_confidence:
            if self._trigger(stop_match.phrase, msg.utterance_id):
                self._on_stop_phrase(stop_match.phrase, stop_match.confidence)
            return

        if self._backend == "oww":
            # ASR работает только после admission. Имя в его партиале не
            # должно второй раз открывать окно/обходить output guards FSM.
            return

        activation_match = self._activation_spotter.find(msg.text, leading=speaking)
        if (
            activation_match is not None
            and activation_match.confidence >= min_confidence
            and self._trigger(activation_match.phrase, msg.utterance_id)
        ):
            self._on_activation_phrase(activation_match.phrase, activation_match.confidence)

    def _trigger(self, phrase: str, utterance_id: int) -> bool:
        """Одно срабатывание на utterance_id, плюс refractory_ms между разными."""
        if self._last_trigger_utterance.get(phrase) == utterance_id:
            return False
        refractory_s = float(self.get_parameter("refractory_ms").value) / 1000.0
        now = time.monotonic()
        last = self._last_trigger_at.get(phrase, -float("inf"))
        if now - last < refractory_s:
            return False
        self._last_trigger_at[phrase] = now
        self._last_trigger_utterance[phrase] = utterance_id
        return True

    # -- срабатывание -----------------------------------------------------

    def _on_stop_phrase(self, phrase: str, confidence: float) -> None:
        """Стоп-слово -- L1: CancelAll + wakeword."""
        self._publish_wakeword(phrase, confidence)
        self._publish_cancel_all(phrase)

    def _on_activation_phrase(self, phrase: str, confidence: float) -> None:
        """Активация («Фирая») -- wakeword; CancelAll чтобы прервать рассказ/реплику."""
        self._publish_wakeword(phrase, confidence)
        self._publish_cancel_all(phrase)

    def _publish_cancel_all(self, phrase: str) -> None:
        cancel = CancelAll()
        cancel.stamp = self.get_clock().now().to_msg()
        cancel.epoch = self.get_clock().now().nanoseconds
        cancel.scope = CancelAll.SCOPE_ALL
        cancel.reason = CancelAll.REASON_WAKEWORD
        self._cancel_pub.publish(cancel)
        self.get_logger().info(f"wakeword {phrase!r}: публикую CancelAll")

    def _publish_wakeword(self, phrase: str, confidence: float) -> None:
        self._triggers_total += 1
        msg = Wakeword()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.keyword = phrase
        msg.confidence = confidence
        msg.tts_active = self._tts_active()
        msg.azimuth = float("nan")
        self._wakeword_pub.publish(msg)
        self.get_logger().info(
            f"wakeword: {phrase!r} confidence={confidence:.2f} tts_active={msg.tts_active}"
        )

    # -- диагностика ------------------------------------------------------

    def _publish_diagnostics(self) -> None:
        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()
        entry = DiagnosticStatus(
            name="voice/wakeword",
            hardware_id="wakeword_node",
            level=DiagnosticStatus.ERROR if self._runtime_error else DiagnosticStatus.OK,
            message=self._runtime_error or ("armed" if self._backend == "oww" else "idle"),
            values=[
                KeyValue(key="triggers_total", value=str(self._triggers_total)),
                KeyValue(key="stale_speaking_warnings", value=str(self._stale_speaking_warnings)),
                KeyValue(key="backend", value=self._backend),
                KeyValue(key="model_sha256", value=self._model_sha256),
                KeyValue(
                    key="last_score",
                    value=str(
                        self._oww_stream.last_score if self._oww_stream is not None else 0.0
                    ),
                ),
                KeyValue(
                    key="capture_resets",
                    value=str(
                        self._oww_stream.discontinuities if self._oww_stream is not None else 0
                    ),
                ),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = WakewordNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
