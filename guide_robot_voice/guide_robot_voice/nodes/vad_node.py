"""Детекция речевой активности на Silero VAD.

Гистерезис здесь не украшение. Голый порог на вероятности даёт дребезг
на границе, а каждый переход в active -- это потенциальный barge-in,
то есть обрыв речи робота. Дребезг на пороге выглядит для посетителя
как заикающийся робот. Отсюда разные пороги на вход и выход плюс
минимальные длительности состояний.

min_silence_ms задаёт и запасной end-of-turn: пока turn_detector
не реализован, именно эта пауза закрывает высказывание. Для экскурсионных
вопросов ("а сколько ему лет?") 700 мс закрывают задачу целиком.
"""

from __future__ import annotations

import numpy as np
import rclpy
from guide_robot_msgs.msg import AudioChunk, CancelAll, SpeakingStatus, VoiceActivity
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from std_msgs.msg import Empty

QOS_COMMAND = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)


class VadNode(LifecycleNode):
    """Silero VAD с гистерезисом и генерацией barge-in."""

    def __init__(self) -> None:
        """Объявить параметры."""
        super().__init__("vad_node")
        self.declare_parameter("model_path", "")
        self.declare_parameter("window_samples", 512)
        self.declare_parameter("enter_threshold", 0.6)
        self.declare_parameter("exit_threshold", 0.35)
        self.declare_parameter("min_speech_ms", 120)
        self.declare_parameter("min_silence_ms", 700)
        self.declare_parameter("barge_in_enabled", True)
        self.declare_parameter("barge_in_min_speech_ms", 200)

        self._model = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._active = False
        self._state_samples = 0
        self._sample_rate = 16000
        self._tts_speaking = False
        self._cancel_epoch = 0
        self._barge_in_sent = False

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить модель и поднять интерфейсы."""
        del state
        try:
            import torch

            path = str(self.get_parameter("model_path").value)
            if path:
                self._model = torch.jit.load(path)
            else:
                self._model, _ = torch.hub.load("snakers4/silero-vad", "silero_vad")
            self._model.eval()
            self._torch = torch
        except Exception as error:
            self.get_logger().error(f"не удалось загрузить Silero VAD: {error}")
            return TransitionCallbackReturn.FAILURE

        self._activity_pub = self.create_lifecycle_publisher(
            VoiceActivity, "/speech/vad", qos_profile_sensor_data
        )
        self._barge_in_pub = self.create_lifecycle_publisher(
            Empty, "/speech/barge_in", QOS_COMMAND
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
        """Запомнить, говорит ли робот прямо сейчас."""
        self._tts_speaking = msg.speaking
        if not msg.speaking:
            self._barge_in_sent = False

    def _on_audio(self, msg: AudioChunk) -> None:
        """Прогнать кадр через VAD."""
        self._sample_rate = msg.sample_rate
        pcm = np.asarray(msg.data, dtype=np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, pcm])

        window = int(self.get_parameter("window_samples").value)
        while self._buffer.shape[0] >= window:
            chunk, self._buffer = self._buffer[:window], self._buffer[window:]
            probability = self._infer(chunk)
            self._update(probability, chunk, window, msg)

    def _infer(self, chunk: np.ndarray) -> float:
        tensor = self._torch.from_numpy(chunk)
        with self._torch.no_grad():
            return float(self._model(tensor, self._sample_rate).item())

    def _update(self, probability: float, chunk: np.ndarray, window: int, msg: AudioChunk) -> None:
        """Обновить состояние с гистерезисом и, при необходимости, прервать TTS."""
        enter = float(self.get_parameter("enter_threshold").value)
        exit_threshold = float(self.get_parameter("exit_threshold").value)
        min_speech = int(self.get_parameter("min_speech_ms").value) * self._sample_rate // 1000
        min_silence = int(self.get_parameter("min_silence_ms").value) * self._sample_rate // 1000

        self._state_samples += window
        if not self._active and probability >= enter and self._state_samples >= min_speech:
            self._active, self._state_samples = True, 0
        elif self._active and probability < exit_threshold and self._state_samples >= min_silence:
            self._active, self._state_samples = False, 0

        activity = VoiceActivity()
        activity.header.stamp = msg.header.stamp
        activity.header.frame_id = msg.header.frame_id
        activity.active = self._active
        activity.probability = probability
        activity.state_duration = self._state_samples / self._sample_rate
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        activity.level_dbfs = 20.0 * float(np.log10(rms + 1e-9))
        self._activity_pub.publish(activity)

        self._maybe_barge_in(min_speech)

    def _maybe_barge_in(self, min_speech: int) -> None:
        """Прервать речь робота, если посетитель заговорил поверх неё."""
        if not bool(self.get_parameter("barge_in_enabled").value):
            return
        if not (self._active and self._tts_speaking) or self._barge_in_sent:
            return
        threshold = (
            int(self.get_parameter("barge_in_min_speech_ms").value) * self._sample_rate // 1000
        )
        if self._state_samples < max(threshold - min_speech, 0):
            return

        self._barge_in_sent = True
        self._barge_in_pub.publish(Empty())

        self._cancel_epoch += 1
        cancel = CancelAll()
        cancel.stamp = self.get_clock().now().to_msg()
        cancel.epoch = self._cancel_epoch
        cancel.scope = CancelAll.SCOPE_NARRATION
        cancel.reason = CancelAll.REASON_BARGE_IN
        self._cancel_pub.publish(cancel)


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
