"""Нода детекции ключевого слова (wakeword / стоп-слово).

Два бэкенда за одним параметром backend (design §3.3):
  asr_kws (Stage 1, реализован здесь) -- подписка на /asr/partial,
    нечёткое сравнение по Левенштейну (lib/keyword_spotter.py). Даёт
    стоп-слово бесплатно, ценой задержки ASR (~300-500 мс) и зависимости
    от работающего asr_node.
  oww (Stage 3) -- openWakeWord поверх окон 1280 сэмплов /audio/mic,
    своя модель на синтетике Piper. НЕ реализован в этом шаге -- параметр
    backend принимает значение, но переключения на него нет, ровно как
    aec.* в audio_frontend и oww в дизайн-документе для vad_node.

Стоп-слова -- L1-путь, как и barge-in в vad_node: нода сама публикует
CancelAll(scope=SCOPE_ALL, reason=REASON_WAKEWORD), не дожидаясь mission.
Активационные фразы CancelAll не публикуют -- это не аварийная отмена,
а просто сигнал "посетитель обращается к роботу".

tts_active берётся из последнего /voice/speaking. В отличие от vad_node
и asr_node, протухший статус здесь не просто тихо считается false --
design §3.3 явно требует WARN в диагностику: без tts_active это поле
Wakeword.msg превращается во враньё, а именно по нему считается
false-wake-under-TTS -- приёмочная метрика пакета.
"""

from __future__ import annotations

import threading
import time

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import CancelAll, SpeakingStatus, Transcript, Wakeword
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.keyword_spotter import KeywordSpotter
from guide_robot_voice.lib.qos import (
    QOS_ASR_PARTIAL,
    QOS_CANCEL_ALL,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)

_SPEAKING_STATUS_STALE_SEC = 0.4


class WakewordNode(LifecycleNode):
    """Lifecycle-нода детекции ключевого слова."""

    def __init__(self) -> None:
        """Объявить параметры. Споттеры собираются в on_configure."""
        super().__init__("wakeword_node")

        self.declare_parameter("backend", "asr_kws")
        self.declare_parameter("activation_phrases", ["робот", "слушай робот"])
        self.declare_parameter("stop_phrases", ["стоп", "стой", "хватит", "замолчи"])
        self.declare_parameter("fuzzy_max_distance", 1)
        self.declare_parameter("min_confidence", 0.5)
        self.declare_parameter("refractory_ms", 1500.0)
        self.declare_parameter("frame_id", "mic_array")

        self._activation_spotter: KeywordSpotter | None = None
        self._stop_spotter: KeywordSpotter | None = None
        self._is_active = False
        self._lock = threading.Lock()

        self._latest_speaking: SpeakingStatus | None = None
        self._last_trigger_at: dict[str, float] = {}
        self._triggers_total = 0
        self._stale_speaking_warnings = 0

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
            raise NotImplementedError(
                "backend=oww (openWakeWord, Stage 3) ещё не реализован -- "
                "нужна модель, обученная на синтетике Piper (design §3.3). "
                "Используйте backend=asr_kws (Stage 1)."
            )

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
        self._partial_sub = self.create_subscription(
            Transcript, "/asr/partial", self._on_partial, QOS_ASR_PARTIAL
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
        self._latest_speaking = None
        with self._lock:
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
        self._activation_spotter = None
        self._stop_spotter = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- вход -----------------------------------------------------------

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        self._latest_speaking = msg

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

        min_confidence = float(self.get_parameter("min_confidence").value)

        stop_match = self._stop_spotter.find(msg.text)
        if stop_match is not None and stop_match.confidence >= min_confidence:
            if self._trigger(stop_match.phrase):
                self._on_stop_phrase(stop_match.phrase, stop_match.confidence)
            return

        activation_match = self._activation_spotter.find(msg.text)
        if (
            activation_match is not None
            and activation_match.confidence >= min_confidence
            and self._trigger(activation_match.phrase)
        ):
            self._publish_wakeword(activation_match.phrase, activation_match.confidence)

    def _trigger(self, phrase: str) -> bool:
        """Рефрактерный гейт: одно срабатывание на фразу за refractory_ms."""
        refractory_s = float(self.get_parameter("refractory_ms").value) / 1000.0
        now = time.monotonic()
        last = self._last_trigger_at.get(phrase, -float("inf"))
        if now - last < refractory_s:
            return False
        self._last_trigger_at[phrase] = now
        return True

    # -- срабатывание -----------------------------------------------------

    def _on_stop_phrase(self, phrase: str, confidence: float) -> None:
        """Стоп-слово -- L1: публикуем CancelAll сами, не дожидаясь mission."""
        self._publish_wakeword(phrase, confidence)
        cancel = CancelAll()
        cancel.stamp = self.get_clock().now().to_msg()
        cancel.epoch = self.get_clock().now().nanoseconds
        cancel.scope = CancelAll.SCOPE_ALL
        cancel.reason = CancelAll.REASON_WAKEWORD
        self._cancel_pub.publish(cancel)
        self.get_logger().info(f"стоп-слово {phrase!r}: публикую CancelAll")

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
            level=DiagnosticStatus.OK,
            message="idle",
            values=[
                KeyValue(key="triggers_total", value=str(self._triggers_total)),
                KeyValue(key="stale_speaking_warnings", value=str(self._stale_speaking_warnings)),
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
