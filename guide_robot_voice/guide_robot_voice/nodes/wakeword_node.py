"""Детекция ключевого слова на openWakeWord.

Работает поверх TTS -- то есть именно в тех условиях, где эхо робота
имитирует речь. Каждое срабатывание помечается флагом tts_active,
и метрика false-wake-under-TTS считается прямо из этого топика. Это
и есть приёмочная мера качества AEC: если она растёт, проблема
в акустике, а не в пороге детектора.

refractory_ms гасит повторные срабатывания на одном слове: без него
одна фраза посетителя даёт три отмены подряд.
"""

from __future__ import annotations

import math
import time

import numpy as np
import rclpy
from guide_robot_msgs.msg import AudioChunk, CancelAll, SpeakingStatus, Wakeword
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

QOS_COMMAND = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)

# openWakeWord работает кадрами по 80 мс при 16 кГц. Значение задано моделью.
OWW_FRAME_SAMPLES = 1280


class WakewordNode(LifecycleNode):
    """openWakeWord поверх потока /audio/chunk."""

    def __init__(self) -> None:
        """Объявить параметры."""
        super().__init__("wakeword_node")
        self.declare_parameter("model_paths", [""])
        self.declare_parameter("threshold", 0.6)
        self.declare_parameter("threshold_under_tts", 0.8)
        self.declare_parameter("refractory_ms", 1500)
        self.declare_parameter("cancel_scope", int(CancelAll.SCOPE_ALL))

        self._model = None
        self._tts_speaking = False
        self._last_fire = 0.0
        self._epoch = 0
        self._buffer = np.zeros(0, dtype=np.int16)

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить модели и поднять интерфейсы."""
        del state
        try:
            from openwakeword.model import Model

            paths = [p for p in self.get_parameter("model_paths").value if p]
            self._model = Model(wakeword_models=paths) if paths else Model()
        except Exception as error:
            self.get_logger().error(f"не удалось загрузить openWakeWord: {error}")
            return TransitionCallbackReturn.FAILURE

        self._wakeword_pub = self.create_lifecycle_publisher(
            Wakeword, "/speech/wakeword", QOS_COMMAND
        )
        self._cancel_pub = self.create_lifecycle_publisher(
            CancelAll, "/speech/cancel_all", QOS_COMMAND
        )
        self.create_subscription(
            AudioChunk, "/audio/chunk", self._on_audio, qos_profile_sensor_data
        )
        self.create_subscription(
            SpeakingStatus, "/voice/is_speaking", self._on_speaking, QOS_COMMAND
        )
        return TransitionCallbackReturn.SUCCESS

    def _on_speaking(self, msg: SpeakingStatus) -> None:
        self._tts_speaking = msg.speaking

    def _on_audio(self, msg: AudioChunk) -> None:
        """Прогнать кадр через детектор."""
        self._buffer = np.concatenate([self._buffer, np.asarray(msg.data, dtype=np.int16)])
        while self._buffer.shape[0] >= OWW_FRAME_SAMPLES:
            chunk = self._buffer[:OWW_FRAME_SAMPLES]
            self._buffer = self._buffer[OWW_FRAME_SAMPLES:]
            scores = self._model.predict(chunk)
            for keyword, score in scores.items():
                self._maybe_fire(keyword, float(score), msg)

    def _maybe_fire(self, keyword: str, score: float, msg: AudioChunk) -> None:
        """Опубликовать срабатывание, если порог пройден и рефрактерность истекла."""
        threshold = float(
            self.get_parameter("threshold_under_tts" if self._tts_speaking else "threshold").value
        )
        if score < threshold:
            return
        now = time.monotonic()
        if (now - self._last_fire) * 1e3 < float(self.get_parameter("refractory_ms").value):
            return
        self._last_fire = now

        event = Wakeword()
        event.header.stamp = msg.header.stamp
        event.header.frame_id = msg.header.frame_id
        event.keyword = keyword
        event.confidence = score
        event.tts_active = self._tts_speaking
        event.azimuth = math.nan
        self._wakeword_pub.publish(event)

        self._epoch += 1
        cancel = CancelAll()
        cancel.stamp = self.get_clock().now().to_msg()
        cancel.epoch = self._epoch
        cancel.scope = int(self.get_parameter("cancel_scope").value)
        cancel.reason = CancelAll.REASON_WAKEWORD
        self._cancel_pub.publish(cancel)


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
