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
   добавляется в накопитель. GigaAM крутится на отдельном потоке: таймер
   на том же executor'е, что и подписка KEEP_LAST, на сотни мс глушил
   /audio/mic, и фраза приезжала в декодер с дырами.
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
import queue
import threading
import time
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _DecodeJob:
    """Снимок высказывания для декода вне executor'а."""

    kind: str
    pcm: np.ndarray
    utterance_id: int
    timestamp: float
    prefix_samples: int
    total_samples: int
    speech_ms: float


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
        # При gate_on_tts: всё равно копить окно и слать /asr/partial во время
        # TTS (для wakeword «робот»/«стоп»), финалы в диалог не открывать.
        self.declare_parameter("wakeword_listen_during_tts", False)
        self.declare_parameter("frame_id", "mic_array")

        self._asr: GigaAmCtc | None = None
        self._turn_policy: TurnPolicy | None = None
        self._pre_roll: RingBuffer | None = None
        self._is_active = False
        self._lock = threading.Lock()

        self._utterance_id = 0
        self._utterance_open = False
        self._utterance_chunks: list[np.ndarray] = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        """Длина pre-roll внутри накопителя -- utterance_ms считается без неё."""
        self._utterance_timestamp = 0.0
        self._last_partial_text = ""

        self._latest_speaking: SpeakingStatus | None = None
        self._tts_hold_until = 0.0

        self._utterances_total = 0
        self._finals_published = 0
        self._finals_dropped_short = 0

        self._decode_jobs: queue.Queue[_DecodeJob | None] = queue.Queue()
        self._decode_stop = threading.Event()
        self._decode_worker: threading.Thread | None = None

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
        self._start_decode_worker()

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
        """Остановить воркер декода и освободить модель."""
        del state
        self._stop_decode_worker()
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
            # Хвост shadow-слушания под TTS не должен стать финалом в диалог.
            with self._lock:
                if self._utterance_open:
                    self._close_utterance()

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

    def _wakeword_listen_during_tts(self) -> bool:
        return bool(self.get_parameter("wakeword_listen_during_tts").value)

    def _on_audio(self, msg: AudioChunk) -> None:
        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        samples = np.array(msg.data, dtype=np.int16)
        with self._lock:
            if not self._is_active:
                return
            assert self._pre_roll is not None
            self._pre_roll.push(timestamp, samples)
            if self._utterance_open:
                self._utterance_chunks.append(samples)
                self._utterance_samples += samples.shape[0]

    def _on_partial_timer(self) -> None:
        """Поставить партиал в очередь воркера, не декодировать на executor'е."""
        gate_on_tts = bool(self.get_parameter("gate_on_tts").value)
        if gate_on_tts and self._tts_blocks_listen() and not self._wakeword_listen_during_tts():
            return
        self._submit_decode("partial")

    def _on_vad(self, msg: VoiceActivity) -> None:
        assert self._turn_policy is not None
        gate_on_tts = bool(self.get_parameter("gate_on_tts").value)
        tts_blocks = gate_on_tts and self._tts_blocks_listen()
        wakeword_shadow = tts_blocks and self._wakeword_listen_during_tts()

        with self._lock:
            if not self._is_active:
                return
            if tts_blocks and not wakeword_shadow:
                if self._utterance_open:
                    self.get_logger().warning(
                        f"gate_on_tts топит высказывание {self._utterance_id} "
                        f"({self._utterance_speech_ms():.0f}мс речи, последний партиал "
                        f"{self._last_partial_text!r})"
                    )
                    self._close_utterance()
                return
            if wakeword_shadow:
                # Только окно для /asr/partial → wakeword; финалов нет.
                if not self._utterance_open:
                    if msg.active:
                        self._open_utterance()
                    return
                self._trim_utterance_to_partial_window_locked()
                return
            if not self._utterance_open:
                if msg.active:
                    self._open_utterance()
                return
            silence_ms = 0.0 if msg.active else msg.state_duration * 1000.0
            utterance_ms = self._utterance_speech_ms()
            last_partial = self._last_partial_text
            should = self._turn_policy.should_finalize(last_partial, silence_ms, utterance_ms)
        if should:
            self._submit_decode("final")

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
        self._utterances_total += 1

    def _close_utterance(self) -> None:
        self._utterance_open = False
        self._utterance_chunks = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        self._last_partial_text = ""

    def _trim_utterance_to_partial_window_locked(self) -> None:
        """Держать только хвост partial_window_s (shadow-listen под TTS)."""
        window_samples = int(float(self.get_parameter("partial_window_s").value) * _SAMPLE_RATE)
        while self._utterance_samples > window_samples and self._utterance_chunks:
            dropped = self._utterance_chunks.pop(0)
            n = int(dropped.shape[0])
            self._utterance_samples -= n
            self._prefix_samples = max(0, self._prefix_samples - n)

    def _utterance_speech_ms(self) -> float:
        spoken_samples = max(0, self._utterance_samples - self._prefix_samples)
        return spoken_samples / _SAMPLE_RATE * 1000.0

    def _utterance_pcm(self) -> np.ndarray:
        if not self._utterance_chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(self._utterance_chunks)

    def _start_decode_worker(self) -> None:
        self._decode_stop.clear()
        self._decode_worker = threading.Thread(
            target=self._decode_loop, name="asr_decode", daemon=True
        )
        self._decode_worker.start()

    def _stop_decode_worker(self) -> None:
        self._decode_stop.set()
        self._decode_jobs.put(None)
        if self._decode_worker is not None:
            self._decode_worker.join(timeout=5.0)
            self._decode_worker = None

    def _submit_decode(self, kind: str) -> None:
        """Снимок PCM под замком, декод на воркере. Партиал не копится, если воркер занят."""
        if kind == "partial" and self._decode_jobs.qsize() > 0:
            return
        window_s = float(self.get_parameter("partial_window_s").value)
        with self._lock:
            if not self._is_active or not self._utterance_open:
                return
            pcm = self._utterance_pcm()
            if kind == "partial":
                window_samples = int(window_s * _SAMPLE_RATE)
                pcm = pcm[-window_samples:] if pcm.shape[0] > window_samples else pcm
                if pcm.size == 0:
                    return
            job = _DecodeJob(
                kind,
                pcm,
                self._utterance_id,
                self._utterance_timestamp,
                self._prefix_samples,
                self._utterance_samples,
                self._utterance_speech_ms(),
            )
            if kind == "final":
                self._close_utterance()
        self._decode_jobs.put(job)

    def _decode_loop(self) -> None:
        while not self._decode_stop.is_set():
            try:
                job = self._decode_jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self._run_decode(job)
            except Exception as error:
                self.get_logger().error(f"сбой декода ASR: {error}")

    def _run_decode(self, job: _DecodeJob) -> None:
        assert self._asr is not None
        result = self._asr.decode(job.pcm) if job.pcm.size else None
        text = result.text.strip() if result is not None else ""
        confidence = result.confidence if result is not None else -1.0

        if job.kind == "partial":
            with self._lock:
                if not self._utterance_open or job.utterance_id != self._utterance_id:
                    return
                self._last_partial_text = text
            self._publish_transcript(text, confidence, is_final=False, job=job)
            return

        min_chars = int(self.get_parameter("min_final_chars").value)
        if len(text) < min_chars:
            self._finals_dropped_short += 1
            self.get_logger().info(f"drop {text!r} {job.speech_ms:.0f}ms")
            return

        self.get_logger().info(f"final {text!r} {job.speech_ms:.0f}ms")
        self._publish_transcript(text, confidence, is_final=True, job=job)
        self._finals_published += 1

    def _publish_transcript(
        self,
        text: str,
        confidence: float,
        *,
        is_final: bool,
        job: _DecodeJob,
    ) -> None:
        msg = Transcript()
        msg.header.stamp = self._seconds_to_time_msg(job.timestamp)
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.utterance_id = job.utterance_id
        msg.text = text
        msg.is_final = is_final
        msg.confidence = confidence
        msg.speech_start = job.prefix_samples / _SAMPLE_RATE
        msg.speech_end = job.total_samples / _SAMPLE_RATE
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
