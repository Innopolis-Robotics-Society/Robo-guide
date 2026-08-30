"""Нода распознавания речи (ASR).

Собирает вместе GigaAM v3 CTC (lib/asr_model.py) и политику конца хода
(lib/turn_policy.py). Сама нода отвечает за ROS-обвязку: накопление
высказывания по сигналу /vad, pre-roll, троттлинг партиалов, вызов
политики на каждом обновлении /vad и публикацию Transcript.

ПОТОК (design §3.4, с поправкой на §-отклонение в lib/asr_model.py):
1. Кадры /audio/mic всегда копятся в кольцевой pre-roll буфер
   (pre_roll_ms), независимо от состояния VAD.
2. /vad active=false -> true, ранее не было открытого высказывания,
   TTS не гейтит (gate_on_tts) -- открывается высказывание: utterance_id++,
   в накопитель высказывания подаётся снимок pre-roll (без него срезается
   первый слог -- design §3.4).
3. Каждый новый кадр /audio/mic во время открытого высказывания
   добавляется в накопитель. Партиалы считает таймер (partial_rate_hz),
   не колбэк микрофона: OfflineRecognizer на том же executor'е иначе
   не забирает /audio/mic (depth KEEP_LAST) и VAD видит дыры first_sample.
   Сам decode() (5с окно, десятки-сотни мс на CPU) всё ещё делил бы
   executor с _on_audio, будь он на таймере/vad-колбэке напрямую --
   вынесен в отдельный ThreadPoolExecutor(1) (stage3 B2), НЕ
   MultiThreadedExecutor: один декодер, одна очередь, партиалы --
   depth-1/drop-oldest (если предыдущий партиал ещё не готов, новый
   тик таймера просто пропускается, а не копится в очереди), финалы --
   всегда исполняются, снимок нужных полей высказывания берётся ДО
   close_utterance(), чтобы асинхронный decode не зацепил уже
   следующее высказывание.
4. Каждое /vad-сообщение во время открытого высказывания прогоняется
   через TurnPolicy.should_finalize(). Тишина берётся из state_duration
   самого /vad -- vad_node уже считает её точно, задваивать незачем.
5. На финализации -- ОДИН проход OfflineRecognizer по ВСЕМУ накопителю
   (спешить некуда, высказывание уже закончено). Короче min_final_chars --
   не публикуется вовсе (шум/лязг, а не речь, симметрично min_speech_ms
   в vad_node).
"""

from __future__ import annotations

import pathlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import NamedTuple

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import AudioChunk, SpeakingStatus, Transcript, VoiceActivity
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.asr_model import GigaAmCtc
from guide_robot_voice.lib.qos import (
    QOS_ASR_PARTIAL,
    QOS_ASR_TRANSCRIPT,
    QOS_AUDIO_MIC,
    QOS_VAD,
    QOS_VOICE_SPEAKING,
)
from guide_robot_voice.lib.ring import RingBuffer
from guide_robot_voice.lib.turn_policy import TurnPolicy, TurnPolicyConfig

_SAMPLE_RATE = 16000
_SPEAKING_STATUS_STALE_SEC = 0.4
_TTS_ECHO_HOLD_S = 1.0


class _UtteranceSnapshot(NamedTuple):
    """Поля высказывания, снятые ДО close_utterance() -- для decode() в фоновом потоке."""

    utterance_id: int
    timestamp: float
    prefix_samples: int
    utterance_samples: int


class AsrNode(LifecycleNode):
    """Lifecycle-нода распознавания речи."""

    def __init__(self) -> None:
        """Объявить параметры. Модель загружается в on_configure."""
        super().__init__("asr_node")

        self.declare_parameter("model_path", "")
        self.declare_parameter("tokens_path", "")
        self.declare_parameter("num_threads", 2)
        self.declare_parameter("pre_roll_ms", 300.0)
        self.declare_parameter("partial_rate_hz", 6.0)
        # НЕ из design §3.4 -- добавлено из-за отсутствия честного стриминга
        # у GigaAM в sherpa-onnx, см. lib/asr_model.py.
        self.declare_parameter("partial_window_s", 5.0)
        self.declare_parameter("base_silence_ms", 600.0)
        self.declare_parameter("short_silence_ms", 350.0)
        self.declare_parameter("max_utterance_s", 20.0)
        self.declare_parameter("short_path_max_ms", 2500.0)
        self.declare_parameter("min_final_chars", 2)
        self.declare_parameter("gate_on_tts", False)
        self.declare_parameter("frame_id", "mic_array")

        self._asr: GigaAmCtc | None = None
        self._turn_policy: TurnPolicy | None = None
        self._pre_roll: RingBuffer | None = None
        self._is_active = False
        self._lock = threading.Lock()
        self._decode_executor: ThreadPoolExecutor | None = None
        self._pending_partial: Future | None = None

        self._utterance_id = 0
        self._utterance_open = False
        self._utterance_chunks: list[np.ndarray] = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        """Длина pre-roll внутри накопителя -- utterance_ms считается без неё."""
        self._utterance_timestamp = 0.0
        self._last_partial_text = ""
        self._last_partial_at = 0.0

        self._latest_speaking: SpeakingStatus | None = None
        self._tts_hold_until = 0.0

        self._utterances_total = 0
        self._finals_published = 0
        self._finals_dropped_short = 0

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
        tokens_path = str(self.get_parameter("tokens_path").value)
        if not model_path or not tokens_path:
            raise ValueError(
                "параметры model_path/tokens_path не заданы. Модель лежит "
                "в репозитории (models/gigaam_v3_ctc_int8*, git-lfs), "
                "см. config/voice.yaml"
            )
        for path in (model_path, tokens_path):
            if not pathlib.Path(path).exists():
                raise FileNotFoundError(f"не найден файл модели ASR: {path}")

        self.get_logger().info("загружаю модель ASR (GigaAM v3 CTC)...")
        started = time.monotonic()
        self._asr = GigaAmCtc(
            model_path,
            tokens_path,
            sample_rate=_SAMPLE_RATE,
            num_threads=int(self.get_parameter("num_threads").value),
        )
        self._asr.load()
        self.get_logger().info(f"модель ASR загружена за {(time.monotonic() - started):.1f} с")

        # stage3 B2: decode() -- в отдельном потоке, не на executor'е
        # _on_audio/_on_vad/таймера. Один воркер: два decode() параллельно
        # на одной модели не нужны и не быстрее (CPU-bound), а
        # MultiThreadedExecutor здесь избыточен.
        self._decode_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-decode")

        self._turn_policy = TurnPolicy(
            TurnPolicyConfig(
                base_silence_ms=float(self.get_parameter("base_silence_ms").value),
                short_silence_ms=float(self.get_parameter("short_silence_ms").value),
                max_utterance_s=float(self.get_parameter("max_utterance_s").value),
                short_path_max_ms=float(self.get_parameter("short_path_max_ms").value),
            )
        )
        self.get_logger().info(f"turn_policy {self._turn_policy.config}")

        pre_roll_ms = float(self.get_parameter("pre_roll_ms").value)
        pre_roll_samples = int(_SAMPLE_RATE * pre_roll_ms / 1000.0)
        self._pre_roll = RingBuffer(_SAMPLE_RATE, max_samples=pre_roll_samples)

        self._partial_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/partial", QOS_ASR_PARTIAL
        )
        self._transcript_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/transcript", QOS_ASR_TRANSCRIPT
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._mic_sub = self.create_subscription(
            AudioChunk, "/audio/mic", self._on_audio, QOS_AUDIO_MIC
        )
        self._vad_sub = self.create_subscription(VoiceActivity, "/vad", self._on_vad, QOS_VAD)
        self._speaking_sub = self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speaking_status, QOS_VOICE_SPEAKING
        )
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)
        partial_hz = float(self.get_parameter("partial_rate_hz").value)
        self._partial_timer = self.create_timer(1.0 / max(partial_hz, 0.1), self._on_partial_timer)

        self.get_logger().info("asr_node сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Сбросить операционное состояние и начать обработку."""
        pre_roll_ms = float(self.get_parameter("pre_roll_ms").value)
        pre_roll_samples = int(_SAMPLE_RATE * pre_roll_ms / 1000.0)
        self._pre_roll = RingBuffer(_SAMPLE_RATE, max_samples=pre_roll_samples)
        self._close_utterance()
        self._latest_speaking = None
        self._tts_hold_until = 0.0
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
        if self._decode_executor is not None:
            # wait=True -- дождаться, чтобы decode() в полёте не обратился
            # к self._asr после close() ниже.
            self._decode_executor.shutdown(wait=True, cancel_futures=True)
            self._decode_executor = None
        self._pending_partial = None
        if self._asr is not None:
            self._asr.close()
            self._asr = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- вход ---------------------------------------------------------------

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        prev = self._latest_speaking
        self._latest_speaking = msg
        if prev is not None and prev.speaking and not msg.speaking:
            self._tts_hold_until = time.monotonic() + _TTS_ECHO_HOLD_S

    def _is_tts_speaking(self) -> bool:
        status = self._latest_speaking
        if status is None or not status.speaking:
            return False
        stamp = status.stamp.sec + status.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        return age <= _SPEAKING_STATUS_STALE_SEC

    def _tts_blocks_listen(self) -> bool:
        if self._is_tts_speaking():
            return True
        return time.monotonic() < self._tts_hold_until

    def _on_audio(self, msg: AudioChunk) -> None:
        with self._lock:
            if not self._is_active:
                return

        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        samples = np.array(msg.data, dtype=np.int16)
        assert self._pre_roll is not None
        self._pre_roll.push(timestamp, samples)

        if not self._utterance_open:
            return

        self._utterance_chunks.append(samples)
        self._utterance_samples += samples.shape[0]

    def _on_partial_timer(self) -> None:
        """Декод партиала вне колбэка /audio/mic: иначе GigaAM держит читателя."""
        with self._lock:
            if not self._is_active or not self._utterance_open:
                return
        if bool(self.get_parameter("gate_on_tts").value) and self._tts_blocks_listen():
            return
        self._maybe_publish_partial()

    def _on_vad(self, msg: VoiceActivity) -> None:
        with self._lock:
            if not self._is_active:
                return
        assert self._turn_policy is not None

        gate_on_tts = bool(self.get_parameter("gate_on_tts").value)
        if gate_on_tts and self._tts_blocks_listen():
            if self._utterance_open:
                # stage3 B4: без AEC своя речь эхом попадает в ASR --
                # gate_on_tts молча топит открытое высказывание, включая
                # РЕАЛЬНУЮ речь посетителя, случайно совпавшую по времени
                # с TTS (barge-in без резюме). Минимальный фикс -- WARN с
                # последним известным партиалом (полного decode() здесь не
                # делаем, чтобы не тащить обратно блокирующий вызов, от
                # которого только что избавились в B2); буфер-и-повтор
                # после TTS -- решение на потом, ждёт аппаратного AEC
                # (XVF3800), который снимет gate_on_tts целиком.
                self.get_logger().warning(
                    f"gate_on_tts топит высказывание {self._utterance_id} "
                    f"({self._utterance_speech_ms():.0f}мс речи, последний партиал "
                    f"{self._last_partial_text!r})"
                )
                self._close_utterance()
            return
        if not self._utterance_open:
            if msg.active:
                self._open_utterance()
            return

        silence_ms = 0.0 if msg.active else msg.state_duration * 1000.0
        utterance_ms = self._utterance_speech_ms()
        if self._turn_policy.should_finalize(self._last_partial_text, silence_ms, utterance_ms):
            self._finalize_utterance()

    # -- высказывание ---------------------------------------------------

    def _open_utterance(self) -> None:
        assert self._pre_roll is not None
        snapshot = self._pre_roll.snapshot()
        if snapshot is None:
            prefix_timestamp, prefix = 0.0, np.zeros(0, dtype=np.int16)
        else:
            prefix_timestamp, prefix = snapshot

        self._utterance_id += 1
        self._utterance_open = True
        self._utterance_chunks = [prefix] if prefix.size else []
        self._utterance_samples = int(prefix.shape[0])
        self._prefix_samples = int(prefix.shape[0])
        self._utterance_timestamp = prefix_timestamp
        self._last_partial_text = ""
        self._last_partial_at = 0.0
        self._utterances_total += 1

    def _close_utterance(self) -> None:
        self._utterance_open = False
        self._utterance_chunks = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        self._last_partial_text = ""
        self._last_partial_at = 0.0

    def _utterance_speech_ms(self) -> float:
        spoken_samples = max(0, self._utterance_samples - self._prefix_samples)
        return spoken_samples / _SAMPLE_RATE * 1000.0

    def _utterance_pcm(self) -> np.ndarray:
        if not self._utterance_chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(self._utterance_chunks)

    def _snapshot(self) -> _UtteranceSnapshot:
        return _UtteranceSnapshot(
            self._utterance_id, self._utterance_timestamp, self._prefix_samples,
            self._utterance_samples,
        )

    def _maybe_publish_partial(self) -> None:
        assert self._asr is not None
        assert self._decode_executor is not None
        rate_hz = float(self.get_parameter("partial_rate_hz").value)
        now = time.monotonic()
        if now - self._last_partial_at < 1.0 / rate_hz:
            return
        if self._pending_partial is not None and not self._pending_partial.done():
            # depth-1, drop-oldest: декодер уже занят предыдущим партиалом --
            # новый тик таймера просто пропускаем, а не копим в очереди.
            return
        self._last_partial_at = now

        window_s = float(self.get_parameter("partial_window_s").value)
        window_samples = int(window_s * _SAMPLE_RATE)
        pcm = self._utterance_pcm()
        windowed = pcm[-window_samples:] if pcm.shape[0] > window_samples else pcm
        if windowed.size == 0:
            return

        snapshot = self._snapshot()
        future = self._decode_executor.submit(self._asr.decode, windowed)
        self._pending_partial = future
        future.add_done_callback(lambda fut: self._on_partial_decoded(fut, snapshot))

    def _on_partial_decoded(self, future: Future, snapshot: _UtteranceSnapshot) -> None:
        """Колбэк decode(): выполняется на потоке ThreadPoolExecutor, не на executor'е ноды."""
        try:
            result = future.result()
        except Exception as error:  # страховка воркера, а не пробрасывать в executor
            self.get_logger().warning(f"декод партиала не удался: {error}")
            return
        if self._utterance_id != snapshot.utterance_id:
            # Высказывание сменилось, пока декодировали -- партиал устарел,
            # публиковать нечего (в отличие от финала, партиал не обязан
            # пережить смену высказывания).
            return
        self._last_partial_text = result.text
        self._publish_transcript(result.text, result.confidence, is_final=False, snapshot=snapshot)

    def _finalize_utterance(self) -> None:
        assert self._asr is not None
        assert self._decode_executor is not None
        pcm = self._utterance_pcm()
        snapshot = self._snapshot()
        speech_ms = self._utterance_speech_ms()
        # Снимок выше уже скопировал всё нужное -- закрыть высказывание
        # можно сразу, не дожидаясь decode(): следующее высказывание не
        # обязано ждать фоновый декодер.
        self._close_utterance()

        if not pcm.size:
            self._on_final_decoded(None, snapshot, speech_ms)
            return
        future = self._decode_executor.submit(self._asr.decode, pcm)
        future.add_done_callback(lambda fut: self._on_final_decoded(fut, snapshot, speech_ms))

    def _on_final_decoded(
        self, future: Future | None, snapshot: _UtteranceSnapshot, speech_ms: float
    ) -> None:
        """Колбэк decode(): выполняется на потоке ThreadPoolExecutor, не на executor'е ноды.

        В отличие от партиала -- публикуется ВСЕГДА, даже если новое
        высказывание уже открылось за время decode(): финал уже
        закончившейся речи должен дойти до диалогового слоя, а не
        потеряться молча.
        """
        result = None
        if future is not None:
            try:
                result = future.result()
            except Exception as error:  # страховка воркера
                self.get_logger().warning(f"декод финала не удался: {error}")
        text = result.text.strip() if result is not None else ""
        min_chars = int(self.get_parameter("min_final_chars").value)

        if len(text) < min_chars:
            self._finals_dropped_short += 1
            self.get_logger().info(f"drop {text!r} {speech_ms:.0f}ms")
            return

        confidence = result.confidence if result is not None else -1.0
        self.get_logger().info(f"final {text!r} {speech_ms:.0f}ms")
        self._publish_transcript(text, confidence, is_final=True, snapshot=snapshot)
        self._finals_published += 1

    def _publish_transcript(
        self, text: str, confidence: float, *, is_final: bool, snapshot: _UtteranceSnapshot
    ) -> None:
        msg = Transcript()
        msg.header.stamp = self._seconds_to_time_msg(snapshot.timestamp)
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.utterance_id = snapshot.utterance_id
        msg.text = text
        msg.is_final = is_final
        msg.confidence = confidence
        msg.speech_start = snapshot.prefix_samples / _SAMPLE_RATE
        msg.speech_end = snapshot.utterance_samples / _SAMPLE_RATE
        msg.language = "ru"
        msg.azimuth = float("nan")
        (self._transcript_pub if is_final else self._partial_pub).publish(msg)

    def _seconds_to_time_msg(self, seconds: float) -> object:
        from builtin_interfaces.msg import Time as TimeMsg

        sec = int(seconds)
        nanosec = round((seconds - sec) * 1e9)
        return TimeMsg(sec=sec, nanosec=nanosec)

    # -- диагностика ------------------------------------------------------

    def _publish_diagnostics(self) -> None:
        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()
        entry = DiagnosticStatus(
            name="voice/asr",
            hardware_id="asr_node",
            level=DiagnosticStatus.OK,
            message="listening" if self._utterance_open else "idle",
            values=[
                KeyValue(key="utterances_total", value=str(self._utterances_total)),
                KeyValue(key="finals_published", value=str(self._finals_published)),
                KeyValue(key="finals_dropped_short", value=str(self._finals_dropped_short)),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)


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
