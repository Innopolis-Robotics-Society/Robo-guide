"""Нода голосового чата с LLM.

Замыкает `/asr/transcript -> LLM -> say` поверх контракта `guide_robot_voice`.
Собирает вместе четыре независимо тестируемых куска из `lib/`: бэкенд LLM,
историю диалога, инкрементальный сплиттер клауз и построчный лог ходов.
Сама нода отвечает за ROS-обвязку, гейты приёма транскрипта, epoch-фенсинг
хода и учёт того, что было реально произнесено -- см. design §5.

Модель исполнения (design §8): генерация -- в `threading.Thread`, обмен
с нодой только через `queue.Queue` и `threading.Event` на объекте `Turn`.
Все переходы состояния `Turn` происходят в дренирующем таймере (20 Гц)
и в колбэках результатов action-клиента `say` -- то есть, при
`MultiThreadedExecutor`, потенциально из разных потоков. Один
`threading.Lock` на объект хода защищает эти переходы.

`chat_node` НИКОГДА не публикует `/speech/cancel_all` -- изоляция
L1-контура (VAD barge-in, стоп-слово) от диалогового уровня сохраняется:
дорогая по латентности логика (LLM, сеть) не должна оказаться на пути
аварийной остановки речи.
"""

from __future__ import annotations

import functools
import itertools
import pathlib
import queue
import threading
import time
from dataclasses import dataclass, field

import rclpy
from ament_index_python.packages import get_package_share_directory
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from rclpy.action import ActionClient
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_llm.lib.backends import EchoBackend, LlamaCppBackend, LlmBackend, OpenAIBackend
from guide_robot_llm.lib.history import History
from guide_robot_llm.lib.qos import (
    QOS_ASR_TRANSCRIPT,
    QOS_CANCEL_ALL,
    QOS_SYSTEM_EVENT,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_llm.lib.sentence_splitter import SentenceSplitter, SentenceSplitterConfig
from guide_robot_llm.lib.turn_log import TurnLog
from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll, SpeakingStatus, SystemEvent, Transcript, Wakeword

# Say.Result.status -> имя для лога/диагностики. REJECTED здесь -- терминальный
# исход клаузы (лимит повторов исчерпан), не путать с промежуточным REJECTED,
# который обрабатывается в _on_goal_result() как повод для повтора.
_SAY_STATUS_NAMES: dict[int, str] = {
    Say.Result.STATUS_COMPLETED: "COMPLETED",
    Say.Result.STATUS_PREEMPTED: "PREEMPTED",
    Say.Result.STATUS_CANCELLED: "CANCELLED",
    Say.Result.STATUS_REJECTED: "REJECTED",
    Say.Result.STATUS_FAILED: "FAILED",
}

# Пунктуация, которую допустимо съесть вместе с активационной фразой
# ("робот," / "робот:" -> просто "робот").
_ACTIVATION_TRAILING_PUNCT = ",:;!?—–-"

_SPEAKING_STATUS_STALE_SEC = 0.4


@dataclass
class ClauseGoal:
    """Одна отправленная (или ожидающая отправки) клауза хода."""

    index: int
    text: str
    handle: object | None = None
    result: Say.Result | None = None
    resolved: bool = False
    retries: int = 0


@dataclass
class Turn:
    """Состояние одного хода диалога. Мутируется только под `_turn_lock`."""

    turn_id: int
    epoch: int
    user_text: str
    started_monotonic: float
    splitter: SentenceSplitter
    abort_event: threading.Event = field(default_factory=threading.Event)
    token_queue: queue.Queue = field(default_factory=queue.Queue)
    generated_parts: list[str] = field(default_factory=list)
    clauses: list[ClauseGoal] = field(default_factory=list)
    generation_settled: bool = False
    finalized: bool = False
    draining: bool = False
    interrupted: bool = False
    first_token_at: float | None = None
    done_at: float | None = None
    asr_confidence: float = -1.0
    wakeword: str = ""
    cancel_info: dict | None = None
    cancel_monotonic: float | None = None


class ChatNode(LifecycleNode):
    """Lifecycle-нода голосового чата с LLM."""

    def __init__(self) -> None:
        """Объявить параметры. Ресурсы захватываются в on_configure."""
        super().__init__("chat_node")

        self.declare_parameter("backend", "llama_cpp")
        self.declare_parameter("base_url", "http://127.0.0.1:8080")
        self.declare_parameter("model", "")
        self.declare_parameter("api_key_env", "LLM_API_KEY")
        self.declare_parameter("system_prompt_file", "")
        self.declare_parameter("max_history_turns", 6)
        self.declare_parameter("max_tokens", 256)
        self.declare_parameter("temperature", 0.7)
        self.declare_parameter("stream", True)
        self.declare_parameter("first_token_timeout_s", 3.0)
        self.declare_parameter("request_timeout_s", 20.0)

        self.declare_parameter("require_wakeword", True)
        self.declare_parameter("activation_phrases", ["робот", "слушай робот"])
        self.declare_parameter("strip_activation_phrase", True)
        self.declare_parameter("engagement_timeout_s", 120.0)
        self.declare_parameter("min_chars", 2)
        self.declare_parameter("echo_guard_ms", 0.0)

        self.declare_parameter("say_priority", 40)
        self.declare_parameter("say_scope", int(CancelAll.SCOPE_DIALOG))
        self.declare_parameter("say_max_duration_s", 30.0)
        self.declare_parameter("goal_retry_limit", 3)
        self.declare_parameter("max_clause_chars", 180)
        self.declare_parameter("first_clause_min_chars", 24)

        self.declare_parameter("fallback_phrases", ["Извините, сейчас не могу ответить."])
        self.declare_parameter("interrupted_marker", " [прервано]")
        self.declare_parameter("log_dir", "~/.ros/llm_turns")
        self.declare_parameter("diagnostics_hz", 1.0)

        self._active = False
        self._backend: LlmBackend | None = None
        self._backend_name = ""
        self._model_name = ""
        self._history: History | None = None
        self._turn_log: TurnLog | None = None
        self._system_prompt = ""
        self._splitter_config = SentenceSplitterConfig()

        self._say_priority = 0
        self._say_scope = int(CancelAll.SCOPE_DIALOG)
        self._say_max_duration_s = 0.0
        self._goal_retry_limit = 0
        self._first_token_timeout_s = 0.0
        self._request_timeout_s = 0.0
        self._require_wakeword = True
        self._activation_phrases: list[str] = []
        self._strip_activation_phrase = True
        self._engagement_timeout_s = 0.0
        self._min_chars = 0
        self._echo_guard_ms = 0.0
        self._fallback_phrases: list[str] = ["Извините, сейчас не могу ответить."]
        self._fallback_cycle = itertools.cycle(self._fallback_phrases)

        self._turn_lock = threading.Lock()
        self._current_turn: Turn | None = None
        self._turn_counter = 0
        self._turn_state = "idle"
        self._last_cancel_epoch = 0
        self._last_engagement_ns: int | None = None
        self._last_wakeword_phrase = ""

        self._latest_speaking: SpeakingStatus | None = None
        self._speaking_intervals: list[list[int | None]] = []

        self._turns_total = 0
        self._turns_interrupted = 0
        self._interrupted_recent: list[bool] = []
        self._last_ttft_ms: float | None = None
        self._last_total_ms: float | None = None
        self._backend_ok = True
        self._last_error = ""

        self._cb_reentrant = ReentrantCallbackGroup()
        self._cb_sub = MutuallyExclusiveCallbackGroup()

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять бэкенд и интерфейсы ROS."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._say_priority = int(self.get_parameter("say_priority").value)
        self._say_scope = int(self.get_parameter("say_scope").value)
        self._say_max_duration_s = float(self.get_parameter("say_max_duration_s").value)
        self._goal_retry_limit = int(self.get_parameter("goal_retry_limit").value)
        self._first_token_timeout_s = float(self.get_parameter("first_token_timeout_s").value)
        self._request_timeout_s = float(self.get_parameter("request_timeout_s").value)
        self._require_wakeword = bool(self.get_parameter("require_wakeword").value)
        self._activation_phrases = [
            str(p) for p in self.get_parameter("activation_phrases").value
        ]
        self._strip_activation_phrase = bool(self.get_parameter("strip_activation_phrase").value)
        self._engagement_timeout_s = float(self.get_parameter("engagement_timeout_s").value)
        self._min_chars = int(self.get_parameter("min_chars").value)
        self._echo_guard_ms = float(self.get_parameter("echo_guard_ms").value)
        configured_phrases = [str(p) for p in self.get_parameter("fallback_phrases").value]
        self._fallback_phrases = configured_phrases or ["Извините, сейчас не могу ответить."]
        self._fallback_cycle = itertools.cycle(self._fallback_phrases)

        self._splitter_config = SentenceSplitterConfig(
            max_clause_chars=int(self.get_parameter("max_clause_chars").value),
            first_clause_min_chars=int(self.get_parameter("first_clause_min_chars").value),
        )

        self._system_prompt = self._load_system_prompt()

        self._backend = self._build_backend()
        self._backend_name = str(self.get_parameter("backend").value)
        self._model_name = str(self.get_parameter("model").value)
        ok, detail = self._backend.health()
        if not ok:
            raise RuntimeError(f"бэкенд LLM недоступен: {detail}")
        self._backend_ok = True

        self._history = History(
            max_history_turns=int(self.get_parameter("max_history_turns").value),
            interrupted_marker=str(self.get_parameter("interrupted_marker").value),
        )

        log_dir = str(self.get_parameter("log_dir").value)
        self._turn_log = TurnLog(log_dir)

        self._transcript_sub = self.create_subscription(
            Transcript,
            "/asr/transcript",
            self._on_transcript,
            QOS_ASR_TRANSCRIPT,
            callback_group=self._cb_sub,
        )
        self._wakeword_sub = self.create_subscription(
            Wakeword,
            "/speech/wakeword",
            self._on_wakeword,
            QOS_WAKEWORD,
            callback_group=self._cb_sub,
        )
        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_sub,
        )
        self._speaking_sub = self.create_subscription(
            SpeakingStatus,
            "/voice/speaking",
            self._on_speaking_status,
            QOS_VOICE_SPEAKING,
            callback_group=self._cb_sub,
        )

        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )

        self._say_client = ActionClient(self, Say, "say", callback_group=self._cb_reentrant)

        stream = self.get_parameter("stream").value
        self.get_logger().info(
            f"chat_node сконфигурирован: backend={self._backend_name}, "
            f"require_wakeword={self._require_wakeword}, stream={stream}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Включить обработку и запустить таймеры."""
        self._last_engagement_ns = None
        self._last_wakeword_phrase = ""
        self._latest_speaking = None
        self._speaking_intervals = []
        diagnostics_hz = float(self.get_parameter("diagnostics_hz").value)
        self._drain_timer = self.create_timer(
            1.0 / 20.0, self._on_drain_timer, callback_group=self._cb_reentrant
        )
        self._diag_timer = self.create_timer(
            1.0 / diagnostics_hz, self._publish_diagnostics, callback_group=self._cb_reentrant
        )
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Отключить обработку, отменить активный ход, остановить таймеры.

        Активный ход не дренируется до финализации, как при обычной отмене --
        деактивация означает "этот ход больше никого не интересует", а не
        "подожди результатов". `_current_turn` обнуляется немедленно: иначе
        опоздавшие колбэки результатов say повисли бы навсегда без таймера,
        который их разбирает, и нода намертво отказывала бы всем новым
        транскриптам после повторной активации.
        """
        self._active = False
        with self._turn_lock:
            turn = self._current_turn
            self._current_turn = None
            self._turn_state = "idle"
        if turn is not None:
            turn.abort_event.set()
            for clause in turn.clauses:
                if not clause.resolved and clause.handle is not None:
                    clause.handle.cancel_goal_async()  # type: ignore[attr-defined]
        self.destroy_timer(self._drain_timer)
        self.destroy_timer(self._diag_timer)
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Закрыть бэкенд и лог, снести интерфейсы."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup, дождавшись воркер-потока активного хода."""
        del state
        with self._turn_lock:
            turn = self._current_turn
        if turn is not None:
            turn.abort_event.set()
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        if self._turn_log is not None:
            self._turn_log.close()
            self._turn_log = None
        self._backend = None

    # -- сборка бэкенда ---------------------------------------------------

    def _load_system_prompt(self) -> str:
        path_str = str(self.get_parameter("system_prompt_file").value)
        if not path_str:
            share_dir = get_package_share_directory("guide_robot_llm")
            path_str = f"{share_dir}/config/system_prompt.txt"
        path = pathlib.Path(path_str)
        if not path.exists():
            self.get_logger().warning(
                f"файл системного промпта не найден: {path}, использую пустой"
            )
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _build_backend(self) -> LlmBackend:
        kind = str(self.get_parameter("backend").value)
        stream = bool(self.get_parameter("stream").value)
        if kind == "echo":
            return EchoBackend()
        if kind == "llama_cpp":
            return LlamaCppBackend(
                base_url=str(self.get_parameter("base_url").value),
                model=str(self.get_parameter("model").value),
                max_tokens=int(self.get_parameter("max_tokens").value),
                temperature=float(self.get_parameter("temperature").value),
                stream=stream,
                request_timeout_s=float(self.get_parameter("request_timeout_s").value),
            )
        if kind == "openai":
            return OpenAIBackend(
                base_url=str(self.get_parameter("base_url").value),
                model=str(self.get_parameter("model").value),
                api_key_env=str(self.get_parameter("api_key_env").value),
                max_tokens=int(self.get_parameter("max_tokens").value),
                temperature=float(self.get_parameter("temperature").value),
                stream=stream,
                request_timeout_s=float(self.get_parameter("request_timeout_s").value),
            )
        raise ValueError(f"неизвестный бэкенд LLM: {kind!r}, ожидается llama_cpp | openai | echo")

    # -- гейты приёма транскрипта (design §5.1) ----------------------------

    def _on_transcript(self, msg: Transcript) -> None:
        if not self._active:
            return
        if not msg.is_final:
            return

        cleaned = msg.text.strip()
        if self._strip_activation_phrase:
            cleaned = self._strip_activation_prefix(cleaned)
        if len(cleaned) < self._min_chars:
            return

        now_ns = self.get_clock().now().nanoseconds
        if self._require_wakeword and not self._is_engaged(now_ns):
            self.get_logger().debug("транскрипт отброшен: гейт вовлечённости не пройден")
            return

        if self._echo_guard_ms > 0 and self._is_echo(msg, now_ns):
            self.get_logger().warning("транскрипт отброшен: попадает в окно эхо-гейта")
            return

        with self._turn_lock:
            if self._current_turn is not None:
                self.get_logger().warning("транскрипт отброшен: ход уже активен")
                return
            self._last_engagement_ns = now_ns
            self._start_turn_locked(cleaned, now_ns, msg)

    def _strip_activation_prefix(self, text: str) -> str:
        if not self._activation_phrases:
            return text
        lowered = text.lower()
        best: str | None = None
        for phrase in self._activation_phrases:
            candidate = phrase.strip().lower()
            if candidate and lowered.startswith(candidate):
                if best is None or len(candidate) > len(best):
                    best = candidate
        if best is None:
            return text
        rest = text[len(best) :]
        return rest.lstrip(_ACTIVATION_TRAILING_PUNCT).lstrip()

    def _is_engaged(self, now_ns: int) -> bool:
        if self._last_engagement_ns is None:
            return False
        window_ns = int(self._engagement_timeout_s * 1e9)
        return (now_ns - self._last_engagement_ns) < window_ns

    def _is_echo(self, msg: Transcript, now_ns: int) -> bool:
        header_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        utt_start_ns = header_ns + int(msg.speech_start * 1e9)
        utt_end_ns = header_ns + int(msg.speech_end * 1e9)
        guard_ns = int(self._echo_guard_ms * 1e6)
        for start_ns, end_ns in self._speaking_intervals:
            effective_end_ns = (end_ns if end_ns is not None else now_ns) + guard_ns
            if utt_start_ns < effective_end_ns and utt_end_ns > start_ns:
                return True
        return False

    def _on_wakeword(self, msg: Wakeword) -> None:
        if not self._active:
            return
        self._last_engagement_ns = self.get_clock().now().nanoseconds
        self._last_wakeword_phrase = msg.keyword

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        self._latest_speaking = msg
        now_ns = self.get_clock().now().nanoseconds
        was_speaking = bool(self._speaking_intervals and self._speaking_intervals[-1][1] is None)
        if msg.speaking and not was_speaking:
            self._speaking_intervals.append([now_ns, None])
        elif not msg.speaking and was_speaking:
            self._speaking_intervals[-1][1] = now_ns
        # Не копим интервалы бесконечно -- нужен только недавний хвост
        # для эхо-гейта.
        del self._speaking_intervals[:-8]

    # -- запуск хода --------------------------------------------------------

    def _start_turn_locked(self, user_text: str, epoch_ns: int, msg: Transcript) -> None:
        """Создать и запустить ход. Вызывается уже под `_turn_lock`."""
        self._turn_counter += 1
        turn = Turn(
            turn_id=self._turn_counter,
            epoch=epoch_ns,
            user_text=user_text,
            started_monotonic=time.monotonic(),
            splitter=SentenceSplitter(self._splitter_config),
        )
        turn.asr_confidence = float(msg.confidence)
        turn.wakeword = self._last_wakeword_phrase

        assert self._history is not None
        messages = [{"role": "system", "content": self._system_prompt}]
        messages.extend(self._history.window())
        messages.append({"role": "user", "content": user_text})

        self._current_turn = turn
        self._turn_state = "generating"

        worker = threading.Thread(
            target=self._generate_worker,
            args=(turn, messages),
            daemon=True,
            name=f"chat-gen-{turn.turn_id}",
        )
        worker.start()

    def _generate_worker(self, turn: Turn, messages: list[dict]) -> None:
        """Тело воркер-потока. Никаких вызовов rclpy отсюда -- только очередь."""
        assert self._backend is not None
        error_message: str | None = None
        try:
            for chunk in self._backend.stream(messages, turn.abort_event):
                if turn.abort_event.is_set():
                    break
                if chunk.text:
                    turn.token_queue.put(("chunk", chunk.text))
                if chunk.done:
                    break
        except Exception as error:  # любая ошибка бэкенда обязана дойти до ноды, а не потеряться
            error_message = str(error)
        turn.token_queue.put(("error", error_message) if error_message else ("done", None))

    # -- дренирующий таймер, 20 Гц (design §5.2, §8) ------------------------

    def _on_drain_timer(self) -> None:
        with self._turn_lock:
            turn = self._current_turn
        if turn is None or turn.generation_settled:
            return

        self._drain_queue(turn)

        with self._turn_lock:
            settled = turn.generation_settled
        if settled:
            return

        elapsed = time.monotonic() - turn.started_monotonic
        if turn.first_token_at is None and elapsed > self._first_token_timeout_s:
            self._abort_turn_generation(turn)
            self._handle_generation_failure(
                turn, "llm.timeout", "первый токен не получен вовремя", use_fallback=True
            )
        elif elapsed > self._request_timeout_s:
            self._abort_turn_generation(turn)
            self._settle_generation(turn)

    def _drain_queue(self, turn: Turn) -> None:
        while True:
            try:
                kind, payload = turn.token_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "chunk":
                if turn.first_token_at is None:
                    turn.first_token_at = time.monotonic()
                turn.generated_parts.append(payload)
                self._emit_clauses(turn, turn.splitter.feed(payload))
            elif kind == "done":
                turn.done_at = time.monotonic()
                tail = turn.splitter.flush()
                if tail:
                    self._emit_clauses(turn, [tail])
                self._settle_generation(turn)
                return
            else:  # "error"
                self.get_logger().error(f"генерация оборвалась: {payload}")
                use_fallback = len(turn.clauses) == 0
                self._handle_generation_failure(
                    turn, "llm.backend", str(payload), use_fallback=use_fallback
                )
                return

    def _emit_clauses(self, turn: Turn, texts: list[str]) -> None:
        for text in texts:
            clause = ClauseGoal(index=len(turn.clauses), text=text)
            turn.clauses.append(clause)
            self._send_clause_goal(turn, clause)
        if texts and self._turn_state == "generating":
            self._turn_state = "speaking"

    def _handle_generation_failure(
        self, turn: Turn, event_id: str, detail: str, use_fallback: bool
    ) -> None:
        self._backend_ok = event_id != "llm.backend"
        self._last_error = detail
        self._publish_system_event(SystemEvent.ERROR, event_id, detail)
        if use_fallback:
            phrase = next(self._fallback_cycle)
            clause = ClauseGoal(index=len(turn.clauses), text=phrase)
            turn.clauses.append(clause)
            self._send_clause_goal(turn, clause)
        else:
            turn.interrupted = True
        self._settle_generation(turn)

    def _settle_generation(self, turn: Turn) -> None:
        with self._turn_lock:
            turn.generation_settled = True
        self._maybe_finalize(turn)

    def _abort_turn_generation(self, turn: Turn) -> None:
        turn.abort_event.set()
        if turn.done_at is None:
            turn.done_at = time.monotonic()

    # -- отправка клауз на say (design §5.3, §5.4) ---------------------------

    def _send_clause_goal(self, turn: Turn, clause: ClauseGoal) -> None:
        with self._turn_lock:
            if turn is not self._current_turn:
                return
            fenced = turn.epoch < self._last_cancel_epoch or turn.draining
            if fenced:
                clause.resolved = True

        if fenced:
            self.get_logger().warning(f"клауза {clause.index} не отправлена: ход в дренаже")
            self._maybe_finalize(turn)
            return

        goal = Say.Goal()
        goal.text = clause.text
        goal.voice = ""
        goal.priority = self._say_priority
        goal.scope = self._say_scope
        goal.interruptible = True
        goal.max_duration = self._say_max_duration_s

        self.get_logger().debug(
            f"клауза {clause.index} хода {turn.turn_id} -> say: {clause.text!r}"
        )

        send_future = self._say_client.send_goal_async(goal)
        send_future.add_done_callback(functools.partial(self._on_goal_response, turn, clause))

    def _on_goal_response(self, turn: Turn, clause: ClauseGoal, future: object) -> None:
        goal_handle = future.result()  # type: ignore[attr-defined]
        with self._turn_lock:
            if turn is not self._current_turn:
                return
            clause.handle = goal_handle
            accepted = bool(goal_handle.accepted)  # type: ignore[attr-defined]
            if not accepted:
                clause.resolved = True

        if not accepted:
            self.get_logger().warning(f"клауза {clause.index} отклонена на приёме цели say")
            self._maybe_finalize(turn)
            return

        result_future = goal_handle.get_result_async()  # type: ignore[attr-defined]
        result_future.add_done_callback(functools.partial(self._on_goal_result, turn, clause))

    def _on_goal_result(self, turn: Turn, clause: ClauseGoal, future: object) -> None:
        response = future.result()  # type: ignore[attr-defined]
        result: Say.Result = response.result

        resend = False
        with self._turn_lock:
            if turn is not self._current_turn:
                return
            retry_ok = clause.retries < self._goal_retry_limit
            if result.status == Say.Result.STATUS_REJECTED and retry_ok:
                clause.retries += 1
                resend = True
            else:
                clause.result = result
                clause.resolved = True

        if resend:
            self.get_logger().warning(
                f"клауза {clause.index} отклонена tts_node (очередь полна), "
                f"повтор {clause.retries}/{self._goal_retry_limit}"
            )
            self._send_clause_goal(turn, clause)
            return

        if result.status == Say.Result.STATUS_REJECTED:
            self.get_logger().warning(
                f"клауза {clause.index}: лимит повторов ({self._goal_retry_limit}) "
                "исчерпан -- клауза потеряна"
            )
        self._maybe_finalize(turn)

    # -- отмена (design §5.3) ------------------------------------------------

    def _on_cancel_all(self, msg: CancelAll) -> None:
        """Держать коротким: только фенсинг, флаги и постановка отмены целей."""
        if not self._active:
            return
        if not (msg.scope == CancelAll.SCOPE_ALL or msg.scope == self._say_scope):
            return

        handles_to_cancel: list[object] = []
        with self._turn_lock:
            self._last_cancel_epoch = max(self._last_cancel_epoch, msg.epoch)
            turn = self._current_turn
            if turn is None or turn.finalized:
                return
            turn.interrupted = True
            turn.draining = True
            turn.abort_event.set()
            turn.cancel_info = {"reason": msg.reason, "epoch": int(msg.epoch), "latency_ms": None}
            turn.cancel_monotonic = time.monotonic()
            self._turn_state = "draining"
            for clause in turn.clauses:
                if not clause.resolved and clause.handle is not None:
                    handles_to_cancel.append(clause.handle)

        for handle in handles_to_cancel:
            handle.cancel_goal_async()  # type: ignore[attr-defined]

    # -- финализация хода (design §5.2, шаг 8) -------------------------------

    def _maybe_finalize(self, turn: Turn) -> None:
        with self._turn_lock:
            if turn is not self._current_turn or turn.finalized:
                return
            if not turn.generation_settled or not all(c.resolved for c in turn.clauses):
                return
            turn.finalized = True
        self._finalize_turn(turn)

    def _finalize_turn(self, turn: Turn) -> None:
        assert self._history is not None
        spoken_parts: list[str] = []
        goal_statuses: list[str] = []
        any_not_completed = False

        for clause in turn.clauses:
            if clause.result is not None:
                spoken_parts.append(clause.result.spoken_text)
                goal_statuses.append(_SAY_STATUS_NAMES.get(clause.result.status, "UNKNOWN"))
                if clause.result.status != Say.Result.STATUS_COMPLETED:
                    any_not_completed = True
            else:
                goal_statuses.append("DROPPED")
                any_not_completed = True

        interrupted = turn.interrupted or any_not_completed
        # SentenceSplitter отдаёт клаузы уже без разделяющего пробела на границе
        # (feed() режет буфер по концу предложения и strip()-ит обе стороны),
        # поэтому пробел между результатами клауз нужно вернуть явно -- иначе
        # склейка выглядит как "Здравствуйте!Чем могу помочь?" вместо двух
        # отдельных предложений, и именно эта строка идёт в историю для LLM.
        spoken_text = " ".join(part for part in spoken_parts if part)
        generated_text = "".join(turn.generated_parts)

        self._history.append_turn(turn.user_text, spoken_text, interrupted)

        ttft_ms = (
            (turn.first_token_at - turn.started_monotonic) * 1000.0
            if turn.first_token_at is not None
            else None
        )
        gen_ms = (
            (turn.done_at - turn.started_monotonic) * 1000.0 if turn.done_at is not None else None
        )
        speak_ms = (time.monotonic() - turn.started_monotonic) * 1000.0

        cancel_info = turn.cancel_info
        if cancel_info is not None and turn.cancel_monotonic is not None:
            cancel_info["latency_ms"] = (time.monotonic() - turn.cancel_monotonic) * 1000.0

        if self._turn_log is not None:
            self._turn_log.write(
                {
                    "ts": time.time(),
                    "turn_id": turn.turn_id,
                    "epoch": turn.epoch,
                    "user_text": turn.user_text,
                    "asr_confidence": turn.asr_confidence,
                    "wakeword": turn.wakeword,
                    "history_turns": len(self._history),
                    "generated": generated_text,
                    "spoken": spoken_text,
                    "clauses": [c.text for c in turn.clauses],
                    "goal_statuses": goal_statuses,
                    "ttft_ms": ttft_ms,
                    "gen_ms": gen_ms,
                    "speak_ms": speak_ms,
                    "interrupted": interrupted,
                    "cancel": cancel_info,
                    "backend": self._backend_name,
                    "model": self._model_name,
                }
            )

        self._turns_total += 1
        self._interrupted_recent.append(interrupted)
        del self._interrupted_recent[:-10]
        if interrupted:
            self._turns_interrupted += 1
        self._last_ttft_ms = ttft_ms
        self._last_total_ms = speak_ms

        with self._turn_lock:
            self._current_turn = None
            self._turn_state = "idle"

    # -- system_event ---------------------------------------------------

    def _publish_system_event(self, severity: int, event_id: str, detail: str) -> None:
        event = SystemEvent(id=event_id, severity=severity, detail=detail[:200])
        event.header.stamp = self.get_clock().now().to_msg()
        self._event_pub.publish(event)

    # -- диагностика (design §10) --------------------------------------------

    def _publish_diagnostics(self) -> None:
        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()

        ratio = (
            sum(1 for v in self._interrupted_recent if v) / len(self._interrupted_recent)
            if self._interrupted_recent
            else 0.0
        )
        level = DiagnosticStatus.OK
        if not self._backend_ok:
            level = DiagnosticStatus.ERROR
        elif ratio > 0.5:
            level = DiagnosticStatus.WARN

        entry = DiagnosticStatus(
            name="llm/chat",
            hardware_id="chat_node",
            level=level,
            message=self._turn_state,
            values=[
                KeyValue(key="backend", value=self._backend_name),
                KeyValue(key="model", value=self._model_name),
                KeyValue(key="state", value=self._turn_state),
                KeyValue(key="turns_total", value=str(self._turns_total)),
                KeyValue(key="turns_interrupted", value=str(self._turns_interrupted)),
                KeyValue(
                    key="last_ttft_ms",
                    value="" if self._last_ttft_ms is None else f"{self._last_ttft_ms:.1f}",
                ),
                KeyValue(
                    key="last_total_ms",
                    value="" if self._last_total_ms is None else f"{self._last_total_ms:.1f}",
                ),
                KeyValue(key="backend_ok", value=str(self._backend_ok)),
                KeyValue(key="last_error", value=self._last_error),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)


def main(args: list[str] | None = None) -> None:
    """Точка входа. MultiThreadedExecutor -- action-клиент и таймеры делят Reentrant-группу."""
    rclpy.init(args=args)
    node = ChatNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
