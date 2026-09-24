"""Нода распознавания речи (ASR).

Собирает вместе GigaAM v3 CTC (lib/asr_model.py) и политику конца хода
(lib/turn_policy.py). Сама нода отвечает за ROS-обвязку: накопление
высказывания по сигналу /vad, pre-roll, троттлинг партиалов, вызов
политики на каждом обновлении /vad и публикацию Transcript.

ПОТОК (design §3.4, с поправкой на §-отклонение в lib/asr_model.py):
1. Кадры /audio/mic всегда копятся в кольцевой pre-roll буфер
   (pre_roll_ms), независимо от состояния VAD.
2. В legacy-профиле /vad active=false -> true открывает высказывание. В
   session_managed_input-профиле его открывает только UtteranceControl от
   voice_session_manager; utterance_id и onset_sample приходят в решении.
   Pre-roll выбирается по capture sample index, чтобы не потерять и не
   задублировать начало.
3. Каждый новый кадр /audio/mic во время открытого высказывания
   добавляется в накопитель. GigaAM крутится на отдельном потоке: таймер
   на том же executor'е, что и подписка KEEP_LAST, на сотни мс глушил
   /audio/mic, и фраза приезжала в декодер с дырами.
4. Каждое /vad-сообщение во время открытого высказывания прогоняется
   через TurnPolicy.should_finalize(). Тишина берётся из state_duration
   самого /vad -- vad_node уже считает её точно, задваивать незачем.
5. На финализации -- ОДИН проход OfflineRecognizer по ВСЕМУ накопителю.
   В managed-профиле внешний final публикуется только при актуальном ADMIT;
   поздний REJECT fencing-ует уже запущенный worker.
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
from guide_robot_msgs.msg import (
    AudioChunk,
    SpeakingStatus,
    Transcript,
    UtteranceControl,
    UtteranceEvent,
    VoiceActivity,
    Wakeword,
)
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.asr_model import GigaAmCtc, OrtGigaAmCtc
from guide_robot_voice.lib.gap_fill import fill_small_gap
from guide_robot_voice.lib.qos import (
    QOS_ASR_PARTIAL,
    QOS_ASR_TRANSCRIPT,
    QOS_AUDIO_MIC,
    QOS_UTTERANCE_CONTROL,
    QOS_UTTERANCE_EVENT,
    QOS_VAD,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_voice.lib.ring import IndexedAudioRing, RingBuffer
from guide_robot_voice.lib.turn_policy import TurnPolicy, TurnPolicyConfig

_SAMPLE_RATE = 16000
_SPEAKING_STATUS_STALE_SEC = 0.4
_TTS_ECHO_HOLD_S = 1.0
_PREROLL_DECISION_RESERVE_MS = 1000.0
_MAX_ADMISSION_RECORDS = 256


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
    device_session_id: str
    start_sample: int
    end_sample: int
    submitted_at: float = 0.0
    """time.monotonic() постановки в очередь -- для лога задержки финала."""


class AsrNode(LifecycleNode):
    """Lifecycle-нода распознавания речи."""

    def __init__(self) -> None:  # noqa: PLR0915 -- плоское объявление параметров и полей
        """Объявить параметры. Модель загружается в on_configure."""
        super().__init__("asr_node")

        self.declare_parameter("model_path", "")
        self.declare_parameter("tokens_path", "")
        self.declare_parameter("num_threads", 2)
        # "sherpa" -- sherpa-onnx (в образе только CPU), model_path.
        # "onnxruntime" -- onnxruntime-gpu с CUDA, fp32-граф ort_model_path;
        # нет файла или сессия не поднялась -- откат на sherpa.
        self.declare_parameter("asr_backend", "sherpa")
        self.declare_parameter("ort_model_path", "")
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
        self.declare_parameter("session_managed_input", False)
        # При gate_on_tts: всё равно копить окно и слать /asr/partial во время
        # TTS (для wakeword «Фирая»/«стоп»), финалы в диалог не открывать.
        self.declare_parameter("wakeword_listen_during_tts", False)
        # «Фирая» посреди слитной речи: аудио до срабатывания режется до окна
        # партиала, в котором его нашли, + этот запас на задержку декода и
        # доставки; max_after_wake_s -- сколько ещё слушать просьбу, если фон
        # не даёт VAD замолчать (0 -- только общий max_utterance_s).
        self.declare_parameter("wake_trim_margin_s", 1.0)
        self.declare_parameter("max_after_wake_s", 8.0)
        # Пропуск в /audio/mic не длиннее этого -- тишина внутри фразы, а не
        # разрыв захвата, выбрасывающий её целиком. 0 -- выключено.
        self.declare_parameter("max_filled_gap_ms", 100.0)
        self.declare_parameter("frame_id", "mic_array")

        self._asr: GigaAmCtc | OrtGigaAmCtc | None = None
        self._turn_policy: TurnPolicy | None = None
        self._pre_roll: RingBuffer | None = None
        self._indexed_pre_roll: IndexedAudioRing | None = None
        self._is_active = False
        self._lock = threading.Lock()

        self._utterance_id = 0
        self._utterance_open = False
        self._utterance_chunks: list[np.ndarray] = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        """Длина pre-roll внутри накопителя -- utterance_ms считается без неё."""
        self._utterance_timestamp = 0.0
        self._utterance_device_session_id = ""
        self._utterance_start_sample = 0
        self._utterance_next_sample = 0
        self._utterance_decision = UtteranceControl.DECISION_ADMIT
        self._last_partial_text = ""
        self._wake_cap_ms = 0.0
        """utterance_ms, на котором финализировать после «Фирая» (0 -- нет лимита)."""
        self._wake_trims = 0
        self._gaps_filled = 0
        self._max_fill_samples = 0
        self._latest_control_sequence = -1
        self._latest_control_utterance_id = 0
        self._pending_control: UtteranceControl | None = None
        self._admission: dict[tuple[str, int], int] = {}
        self._preroll_underflows = 0

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
        self._max_fill_samples = int(
            float(self.get_parameter("max_filled_gap_ms").value) * _SAMPLE_RATE / 1000
        )
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
        self._asr = self._load_ort_asr(tokens_path)
        if self._asr is None:
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
        indexed_capacity = pre_roll_samples + int(
            _SAMPLE_RATE * _PREROLL_DECISION_RESERVE_MS / 1000.0
        )
        self._indexed_pre_roll = IndexedAudioRing(_SAMPLE_RATE, max_samples=indexed_capacity)

        self._partial_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/partial", QOS_ASR_PARTIAL
        )
        self._transcript_pub = self.create_lifecycle_publisher(
            Transcript, "/asr/transcript", QOS_ASR_TRANSCRIPT
        )
        self._kws_partial_pub = self.create_lifecycle_publisher(
            Transcript, "/voice/kws_partial", QOS_ASR_PARTIAL
        )
        self._utterance_event_pub = self.create_lifecycle_publisher(
            UtteranceEvent, "/voice/utterance_event", QOS_UTTERANCE_EVENT
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._mic_sub = self.create_subscription(
            AudioChunk, "/audio/mic", self._on_audio, QOS_AUDIO_MIC
        )
        self._vad_sub = self.create_subscription(VoiceActivity, "/vad", self._on_vad, QOS_VAD)
        self._speaking_sub = self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speaking_status, QOS_VOICE_SPEAKING
        )
        self._wakeword_sub = self.create_subscription(
            Wakeword, "/speech/wakeword", self._on_wakeword, QOS_WAKEWORD
        )
        self._input_control_sub = self.create_subscription(
            UtteranceControl,
            "/voice/input_control",
            self._on_input_control,
            QOS_UTTERANCE_CONTROL,
        )
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)
        partial_hz = float(self.get_parameter("partial_rate_hz").value)
        self._partial_timer = self.create_timer(1.0 / max(partial_hz, 0.1), self._on_partial_timer)
        self._start_decode_worker()

        self.get_logger().info("asr_node сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def _load_ort_asr(self, tokens_path: str) -> OrtGigaAmCtc | None:
        """onnxruntime-бэкенд, если выбран и поднялся; иначе None (откат на sherpa)."""
        if str(self.get_parameter("asr_backend").value) != "onnxruntime":
            return None
        ort_model_path = str(self.get_parameter("ort_model_path").value)
        if not ort_model_path or not pathlib.Path(ort_model_path).exists():
            self.get_logger().error(
                f"asr_backend=onnxruntime, но нет ort_model_path={ort_model_path!r} "
                "(fp32 gigaam_v3_ctc.onnx, scripts/fetch_asr_model.sh) -- откат на sherpa/CPU"
            )
            return None
        asr = OrtGigaAmCtc(
            ort_model_path,
            tokens_path,
            num_threads=int(self.get_parameter("num_threads").value),
        )
        try:
            asr.load()
        except Exception as error:  # любой сбой CUDA/ORT -> рабочий CPU-путь
            self.get_logger().error(f"onnxruntime ASR не поднялся: {error} -- откат на sherpa/CPU")
            return None
        providers = asr.active_providers
        if "CUDAExecutionProvider" in providers:
            self.get_logger().info(f"ASR: onnxruntime на GPU, провайдеры {providers}")
        else:
            self.get_logger().warning(f"ASR: onnxruntime без CUDA, провайдеры {providers}")
        return asr

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Сбросить операционное состояние и начать обработку."""
        pre_roll_ms = float(self.get_parameter("pre_roll_ms").value)
        pre_roll_samples = int(_SAMPLE_RATE * pre_roll_ms / 1000.0)
        self._pre_roll = RingBuffer(_SAMPLE_RATE, max_samples=pre_roll_samples)
        indexed_capacity = pre_roll_samples + int(
            _SAMPLE_RATE * _PREROLL_DECISION_RESERVE_MS / 1000.0
        )
        self._indexed_pre_roll = IndexedAudioRing(_SAMPLE_RATE, max_samples=indexed_capacity)
        self._close_utterance()
        self._latest_control_sequence = -1
        self._latest_control_utterance_id = 0
        self._pending_control = None
        self._admission.clear()
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
        if (
            prev is not None
            and prev.speaking
            and not msg.speaking
            and bool(self.get_parameter("gate_on_tts").value)
        ):
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

    def _session_managed_input(self) -> bool:
        return bool(self.get_parameter("session_managed_input").value)

    def _on_audio(self, msg: AudioChunk) -> None:
        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        samples = np.array(msg.data, dtype=np.int16)
        first_sample = int(msg.first_sample)
        device_session_id = msg.device_session_id or "unknown-capture-session"
        with self._lock:
            if not self._is_active:
                return
            assert self._pre_roll is not None
            assert self._indexed_pre_roll is not None
            previous_session = self._indexed_pre_roll.device_session_id
            first_sample, samples, timestamp = self._fill_gap_locked(
                device_session_id, first_sample, samples, timestamp
            )
            self._pre_roll.push(timestamp, samples)
            discontinuity = self._indexed_pre_roll.push(
                device_session_id, first_sample, timestamp, samples
            )
            if discontinuity and previous_session:
                # Финал от старой непрерывной истории не может попасть в диалог.
                self._admission.clear()
                if self._utterance_open and self._session_managed_input():
                    self._discard_open_utterance("capture_discontinuity")
                if self._pending_control is not None and (
                    self._pending_control.device_session_id != device_session_id
                    or int(self._pending_control.onset_sample) < first_sample
                ):
                    self._pending_control = None
                if self._pending_control is not None:
                    pending_key = (
                        self._pending_control.device_session_id,
                        int(self._pending_control.utterance_id),
                    )
                    self._admission[pending_key] = int(self._pending_control.decision)
            if self._pending_control is not None and (
                self._pending_control.device_session_id == device_session_id
                and self._indexed_pre_roll.next_sample is not None
                and self._indexed_pre_roll.next_sample >= self._pending_control.onset_sample
            ):
                pending = self._pending_control
                self._pending_control = None
                self._open_managed_utterance(pending)
            if not self._utterance_open:
                return
            if self._session_managed_input():
                if (
                    discontinuity
                    or device_session_id != self._utterance_device_session_id
                    or first_sample > self._utterance_next_sample
                ):
                    self._discard_open_utterance("capture_discontinuity")
                    return
                block_end = first_sample + int(samples.shape[0])
                if block_end <= self._utterance_next_sample:
                    return
                offset = max(0, self._utterance_next_sample - first_sample)
                appended = samples[offset:]
                if appended.size:
                    self._utterance_chunks.append(appended)
                    self._utterance_samples += int(appended.shape[0])
                    self._utterance_next_sample = block_end
            else:
                self._utterance_chunks.append(samples)
                self._utterance_samples += samples.shape[0]

    def _fill_gap_locked(
        self, device_session_id: str, first_sample: int, samples: np.ndarray, timestamp: float
    ) -> tuple[int, np.ndarray, float]:
        """Короткий пропуск внутри той же сессии захвата -- тишина, а не разрыв."""
        assert self._indexed_pre_roll is not None
        if self._indexed_pre_roll.device_session_id != device_session_id:
            return first_sample, samples, timestamp
        first_sample, samples, filled = fill_small_gap(
            self._indexed_pre_roll.next_sample, first_sample, samples, self._max_fill_samples
        )
        if filled:
            self._gaps_filled += 1
        return first_sample, samples, timestamp - filled / _SAMPLE_RATE

    def _on_input_control(self, msg: UtteranceControl) -> None:
        """Применить только самое новое решение для конкретной реплики."""
        key = (msg.device_session_id, int(msg.utterance_id))
        with self._lock:
            if not self._is_active or not self._session_managed_input():
                return
            if (
                int(msg.control_sequence) <= self._latest_control_sequence
                or int(msg.utterance_id) < self._latest_control_utterance_id
            ):
                return
            # Номер выдаёт один manager глобально. Старый utterance/session
            # не может вытеснить новый даже при переупорядочивании DDS.
            self._latest_control_sequence = int(msg.control_sequence)
            self._latest_control_utterance_id = int(msg.utterance_id)
            self._admission[key] = int(msg.decision)
            if len(self._admission) > _MAX_ADMISSION_RECORDS:
                self._admission.pop(next(iter(self._admission)))

            if msg.decision == UtteranceControl.DECISION_REJECT:
                if (
                    self._pending_control is not None
                    and (
                        self._pending_control.device_session_id,
                        int(self._pending_control.utterance_id),
                    )
                    == key
                ):
                    self._pending_control = None
                if (
                    self._utterance_open
                    and self._utterance_device_session_id == msg.device_session_id
                    and self._utterance_id == msg.utterance_id
                ):
                    self._discard_open_utterance(msg.reason or "rejected")
                return

            if self._utterance_open:
                if (
                    self._utterance_device_session_id == msg.device_session_id
                    and self._utterance_id == msg.utterance_id
                ):
                    self._utterance_decision = int(msg.decision)
                    return
                self._discard_open_utterance("superseded")

            if (
                self._indexed_pre_roll is None
                or self._indexed_pre_roll.device_session_id != msg.device_session_id
                or self._indexed_pre_roll.next_sample is None
                or self._indexed_pre_roll.next_sample < int(msg.onset_sample)
            ):
                # Решение и PCM приходят разными DDS-топиками. Не теряем
                # реплику, если control опередил соответствующий audio chunk.
                self._pending_control = msg
                return
            self._pending_control = None
            self._open_managed_utterance(msg)

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
                if msg.active and not self._session_managed_input():
                    self._open_utterance()
                return
            silence_ms = 0.0 if msg.active else msg.state_duration * 1000.0
            utterance_ms = self._utterance_speech_ms()
            last_partial = self._last_partial_text
            should = self._turn_policy.should_finalize(last_partial, silence_ms, utterance_ms)
            if self._wake_cap_ms and utterance_ms >= self._wake_cap_ms:
                # Фоновая речь не даёт тишины -- просьба после «Фирая» не ждёт
                # общего max_utterance_s.
                should = True
        if should:
            self._submit_decode("final")

    def _on_wakeword(self, msg: Wakeword) -> None:
        """«Фирая»/«стоп» внутри открытой фразы -- всё сказанное раньше не к роботу.

        Слитная речь без паузы 600-800 мс копится одной фразой до
        max_utterance_s, и финал нёс в диалог всё, что говорили до
        обращения, а декод 20 с аудио ещё и медленный. Обрезаем накопитель
        до хвоста, в котором wakeword_node нашёл слово (dialog_agent срежет
        остаток текста до «Фирая»), и ограничиваем, сколько ждать просьбу.
        """
        del msg
        keep_s = float(self.get_parameter("partial_window_s").value) + float(
            self.get_parameter("wake_trim_margin_s").value
        )
        max_after_s = float(self.get_parameter("max_after_wake_s").value)
        with self._lock:
            if not self._is_active or not self._utterance_open:
                return
            dropped = self._trim_utterance_locked(int(keep_s * _SAMPLE_RATE))
            if max_after_s > 0:
                self._wake_cap_ms = self._utterance_speech_ms() + max_after_s * 1000.0
            if dropped:
                self._wake_trims += 1
        if dropped:
            self.get_logger().info(
                f"wakeword в фразе {self._utterance_id}: отброшено "
                f"{dropped / _SAMPLE_RATE:.1f} с речи до обращения"
            )

    # -- высказывание ---------------------------------------------------

    def _open_utterance(self) -> None:
        """Открыть legacy-сегмент приблизительным снимком всего pre-roll."""
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
        self._utterance_device_session_id = (
            self._indexed_pre_roll.device_session_id if self._indexed_pre_roll is not None else ""
        )
        indexed_next = (
            self._indexed_pre_roll.next_sample if self._indexed_pre_roll is not None else 0
        )
        self._utterance_next_sample = int(indexed_next or 0)
        self._utterance_start_sample = max(
            0, self._utterance_next_sample - self._utterance_samples
        )
        self._utterance_decision = UtteranceControl.DECISION_ADMIT
        self._last_partial_text = ""
        self._utterances_total += 1

    def _open_managed_utterance(self, control: UtteranceControl) -> None:
        """Открыть segment по точному onset_sample с настраиваемым pre-roll."""
        assert self._indexed_pre_roll is not None
        pre_roll_samples = int(
            _SAMPLE_RATE * float(self.get_parameter("pre_roll_ms").value) / 1000.0
        )
        requested_start = max(0, int(control.onset_sample) - pre_roll_samples)
        snapshot = self._indexed_pre_roll.snapshot_from(control.device_session_id, requested_start)
        if snapshot is None:
            self.get_logger().warning(
                f"нет pre-roll для utterance_id={control.utterance_id}, "
                f"session={control.device_session_id!r}"
            )
            return

        self._utterance_id = int(control.utterance_id)
        self._utterance_open = True
        self._utterance_chunks = [snapshot.samples] if snapshot.samples.size else []
        self._utterance_samples = int(snapshot.samples.shape[0])
        self._prefix_samples = max(
            0,
            min(
                self._utterance_samples,
                int(control.onset_sample) - snapshot.first_sample,
            ),
        )
        self._utterance_timestamp = snapshot.timestamp
        self._utterance_device_session_id = control.device_session_id
        self._utterance_start_sample = snapshot.first_sample
        self._utterance_next_sample = snapshot.next_sample
        self._utterance_decision = int(control.decision)
        self._last_partial_text = ""
        self._utterances_total += 1
        if snapshot.underflow:
            self._preroll_underflows += 1
            self.get_logger().warning(
                f"preroll_underflow: utterance_id={control.utterance_id}, "
                f"requested={requested_start}, available={snapshot.first_sample}"
            )
        self._publish_utterance_event(UtteranceEvent.EVENT_OPENED, "opened")

    def _close_utterance(self) -> None:
        self._utterance_open = False
        self._utterance_chunks = []
        self._utterance_samples = 0
        self._prefix_samples = 0
        self._utterance_device_session_id = ""
        self._utterance_start_sample = 0
        self._utterance_next_sample = 0
        self._utterance_decision = UtteranceControl.DECISION_ADMIT
        self._last_partial_text = ""
        self._wake_cap_ms = 0.0

    def _discard_open_utterance(self, reason: str) -> None:
        if not self._utterance_open:
            return
        self._publish_utterance_event(UtteranceEvent.EVENT_DISCARDED, reason)
        self._close_utterance()

    def _publish_utterance_event(
        self, event: int, status: str, *, job: _DecodeJob | None = None
    ) -> None:
        message = UtteranceEvent()
        message.stamp = self.get_clock().now().to_msg()
        if job is None:
            message.device_session_id = self._utterance_device_session_id
            message.utterance_id = self._utterance_id
            message.start_sample = self._utterance_start_sample
            message.end_sample = self._utterance_next_sample
            message.decision = self._utterance_decision
        else:
            message.device_session_id = job.device_session_id
            message.utterance_id = job.utterance_id
            message.start_sample = job.start_sample
            message.end_sample = job.end_sample
            message.decision = self._admission.get(
                (job.device_session_id, job.utterance_id),
                UtteranceControl.DECISION_REJECT,
            )
        message.event = event
        message.status = status
        self._utterance_event_pub.publish(message)

    def _trim_utterance_to_partial_window_locked(self) -> None:
        """Держать только хвост partial_window_s (shadow-listen под TTS)."""
        window_samples = int(float(self.get_parameter("partial_window_s").value) * _SAMPLE_RATE)
        while self._utterance_samples > window_samples and self._utterance_chunks:
            dropped = self._utterance_chunks.pop(0)
            n = int(dropped.shape[0])
            self._utterance_samples -= n
            self._prefix_samples = max(0, self._prefix_samples - n)

    def _trim_utterance_locked(self, keep_samples: int) -> int:
        """Оставить ровно последние keep_samples накопителя; вернуть, сколько отброшено.

        В отличие от окна под TTS режет с точностью до сэмпла (чанки по
        200-500 мс иначе съели бы начало «Фирая») и сдвигает начало фразы --
        UtteranceEvent/Transcript должны описывать то, что реально декодируется.
        """
        excess = self._utterance_samples - keep_samples
        if excess <= 0:
            return 0
        pcm = self._utterance_pcm()[excess:]
        self._utterance_chunks = [pcm] if pcm.size else []
        self._utterance_samples = int(pcm.shape[0])
        self._prefix_samples = max(0, self._prefix_samples - excess)
        self._utterance_start_sample += excess
        self._utterance_timestamp += excess / _SAMPLE_RATE
        return excess

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
                self._utterance_device_session_id,
                self._utterance_start_sample,
                self._utterance_next_sample,
                time.monotonic(),
            )
            if kind == "final":
                if self._session_managed_input():
                    self._publish_utterance_event(
                        UtteranceEvent.EVENT_ENDPOINT, "endpoint", job=job
                    )
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
        started = time.monotonic()
        result = self._asr.decode(job.pcm) if job.pcm.size else None
        timing = (
            f"очередь {1000 * (started - job.submitted_at):.0f} мс, "
            f"декод {1000 * (time.monotonic() - started):.0f} мс"
        )
        text = result.text.strip() if result is not None else ""
        confidence = result.confidence if result is not None else -1.0

        if job.kind == "partial":
            with self._lock:
                if not self._utterance_open or job.utterance_id != self._utterance_id:
                    return
                self._last_partial_text = text
                decision = self._utterance_decision
                same_session = job.device_session_id == self._utterance_device_session_id
                if self._session_managed_input():
                    if not same_session:
                        return
                    self._publish_transcript(
                        text, confidence, is_final=False, job=job, kws_only=True
                    )
                    if decision != UtteranceControl.DECISION_ADMIT:
                        return
                self._publish_transcript(text, confidence, is_final=False, job=job)
            return

        # Проверка admission и публикация -- один критический участок.
        # Иначе поздний REJECT может проскочить между ними.
        with self._lock:
            decision = self._admission.get(
                (job.device_session_id, job.utterance_id),
                UtteranceControl.DECISION_ADMIT
                if not self._session_managed_input()
                else UtteranceControl.DECISION_REJECT,
            )
            if self._session_managed_input() and decision != UtteranceControl.DECISION_ADMIT:
                self.get_logger().info(
                    f"discard final utterance_id={job.utterance_id}: admission={decision}"
                )
                self._publish_utterance_event(
                    UtteranceEvent.EVENT_DISCARDED, "not_admitted", job=job
                )
                return

            min_chars = int(self.get_parameter("min_final_chars").value)
            if len(text) < min_chars:
                self._finals_dropped_short += 1
                self.get_logger().info(f"drop {text!r} {job.speech_ms:.0f}ms")
                if self._session_managed_input():
                    self._publish_utterance_event(
                        UtteranceEvent.EVENT_DISCARDED, "final_too_short", job=job
                    )
                return

            self.get_logger().info(f"final {text!r} {job.speech_ms:.0f}ms ({timing})")
            self._publish_transcript(text, confidence, is_final=True, job=job)
            self._finals_published += 1
            if self._session_managed_input():
                self._publish_utterance_event(UtteranceEvent.EVENT_FINAL, "final", job=job)

    def _publish_transcript(
        self,
        text: str,
        confidence: float,
        *,
        is_final: bool,
        job: _DecodeJob,
        kws_only: bool = False,
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
        if kws_only:
            self._kws_partial_pub.publish(msg)
        else:
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
                KeyValue(key="preroll_underflows", value=str(self._preroll_underflows)),
                KeyValue(key="wake_trims", value=str(self._wake_trims)),
                KeyValue(key="gaps_filled", value=str(self._gaps_filled)),
                KeyValue(
                    key="session_managed_input",
                    value=str(self._session_managed_input()).lower(),
                ),
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
