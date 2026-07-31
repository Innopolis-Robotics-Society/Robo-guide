"""Распознавание речи в два уровня.

Почему не один faster-whisper. Whisper не потоковый. Партиалы из него
получаются повторным декодом скользящего окна, что даёт нестабильные
префиксы (текст переписывается задним числом) и лишнюю латентность.
Для turn detection и для реакции интерфейса это негодно.

Рабочая схема -- два уровня с разными задачами:

  streaming  Zipformer через sherpa-onnx. Партиалы 5-10 Гц, монотонный
             растущий префикс, тянет на CPU Jetson. Кормит turn_detector
             и индикацию. Точность вторична.

  final      whisper large-v3 на полном сегменте после конца высказывания.
             Один прогон, максимальная точность. Именно этот результат
             уходит в dialog_agent как команда.

Отсюда и разделение топиков: /asr/partial BEST_EFFORT depth 1 -- гипотезы
волатильны, потеря безразлична; /asr/transcript RELIABLE depth 10 -- это
команда. Держать партиалы в RELIABLE-очереди с командами означает
гарантированное разрастание очереди и head-of-line blocking.
"""

from __future__ import annotations

import math
import threading

import numpy as np
import rclpy
from guide_robot_msgs.msg import AudioChunk, Transcript, VoiceActivity
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

QOS_TRANSCRIPT = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
QOS_PARTIAL = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)


class AsrNode(LifecycleNode):
    """Двухуровневое распознавание: потоковые партиалы и точный финал."""

    def __init__(self) -> None:
        """Объявить параметры."""
        super().__init__("asr_node")
        self.declare_parameter("streaming_model_dir", "")
        self.declare_parameter("final_model", "large-v3")
        self.declare_parameter("final_device", "cuda")
        self.declare_parameter("final_compute_type", "float16")
        self.declare_parameter("language", "ru")
        self.declare_parameter("partial_period", 0.15)
        self.declare_parameter("max_utterance_s", 30.0)
        self.declare_parameter("pre_roll_ms", 300)

        self._recognizer = None
        self._stream = None
        self._whisper = None
        self._utterance_id = 0
        self._collecting = False
        self._samples: list[np.ndarray] = []
        self._pre_roll: list[np.ndarray] = []
        self._sample_rate = 16000
        self._lock = threading.Lock()

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить обе модели."""
        del state
        try:
            import sherpa_onnx
            from faster_whisper import WhisperModel

            model_dir = str(self.get_parameter("streaming_model_dir").value)
            if model_dir:
                self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=f"{model_dir}/tokens.txt",
                    encoder=f"{model_dir}/encoder.onnx",
                    decoder=f"{model_dir}/decoder.onnx",
                    joiner=f"{model_dir}/joiner.onnx",
                    sample_rate=16000,
                    feature_dim=80,
                )
                self._stream = self._recognizer.create_stream()

            self._whisper = WhisperModel(
                str(self.get_parameter("final_model").value),
                device=str(self.get_parameter("final_device").value),
                compute_type=str(self.get_parameter("final_compute_type").value),
            )
        except Exception as error:
            self.get_logger().error(f"не удалось загрузить модели ASR: {error}")
            return TransitionCallbackReturn.FAILURE

        self._partial_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/partial", QOS_PARTIAL
        )
        self._final_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/transcript", QOS_TRANSCRIPT
        )
        self.create_subscription(
            AudioChunk, "/audio/chunk", self._on_audio, qos_profile_sensor_data
        )
        self.create_subscription(
            VoiceActivity, "/speech/vad", self._on_vad, qos_profile_sensor_data
        )
        return TransitionCallbackReturn.SUCCESS

    def _on_vad(self, msg: VoiceActivity) -> None:
        """Открыть или закрыть высказывание по границам речи."""
        with self._lock:
            if msg.active and not self._collecting:
                self._collecting = True
                self._utterance_id += 1
                # Pre-roll: VAD подтверждает речь с задержкой, и без него
                # первый слог систематически теряется.
                self._samples = list(self._pre_roll)
            elif not msg.active and self._collecting:
                self._collecting = False
                audio = np.concatenate(self._samples) if self._samples else np.zeros(0)
                self._samples = []
                utterance_id = self._utterance_id
        if not msg.active and audio.size:
            threading.Thread(
                target=self._finalize, args=(audio, utterance_id), daemon=True
            ).start()

    def _on_audio(self, msg: AudioChunk) -> None:
        """Накопить сегмент и обновить потоковую гипотезу."""
        self._sample_rate = msg.sample_rate
        pcm = np.asarray(msg.data, dtype=np.float32) / 32768.0

        pre_roll_frames = int(self.get_parameter("pre_roll_ms").value) * msg.sample_rate // 1000
        with self._lock:
            if self._collecting:
                self._samples.append(pcm)
            else:
                self._pre_roll.append(pcm)
                total = sum(len(p) for p in self._pre_roll)
                while total > pre_roll_frames and len(self._pre_roll) > 1:
                    total -= len(self._pre_roll.pop(0))

        if self._stream is None or not self._collecting:
            return
        self._stream.accept_waveform(msg.sample_rate, pcm)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = self._recognizer.get_result(self._stream)
        if text:
            self._publish(self._partial_pub, text, is_final=False, confidence=-1.0)

    def _finalize(self, audio: np.ndarray, utterance_id: int) -> None:
        """Прогнать полный сегмент через whisper и опубликовать финал."""
        try:
            segments, _ = self._whisper.transcribe(
                audio,
                language=str(self.get_parameter("language").value),
                vad_filter=False,
                beam_size=5,
            )
            parts = list(segments)
        except Exception as error:
            self.get_logger().error(f"ошибка финального распознавания: {error}")
            return

        text = " ".join(p.text.strip() for p in parts).strip()
        if not text:
            return
        confidence = float(np.mean([math.exp(p.avg_logprob) for p in parts])) if parts else -1.0
        self._publish(
            self._final_pub,
            text,
            is_final=True,
            confidence=confidence,
            utterance_id=utterance_id,
            duration=audio.size / self._sample_rate,
        )
        if self._stream is not None:
            self._stream = self._recognizer.create_stream()

    def _publish(
        self,
        publisher: object,
        text: str,
        is_final: bool,
        confidence: float,
        utterance_id: int | None = None,
        duration: float = 0.0,
    ) -> None:
        """Собрать и опубликовать Transcript."""
        msg = Transcript()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "mic_array"
        msg.utterance_id = utterance_id if utterance_id is not None else self._utterance_id
        msg.text = text
        msg.is_final = is_final
        msg.confidence = confidence
        msg.speech_start = 0.0
        msg.speech_end = duration
        msg.language = str(self.get_parameter("language").value)
        msg.azimuth = math.nan
        publisher.publish(msg)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = AsrNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
