"""Нода синтеза речи.

Собирает вместе четыре независимо тестируемых куска из lib/: чанкер,
планировщик, бэкенд синтеза и epoch-fenced сток. Сама нода отвечает
только за ROS-обвязку и за то, чтобы отмена не попала на медленный путь.

Про callback-группы. /speech/cancel_all живёт в отдельной MutuallyExclusive
группе, отличной от группы исполнения целей. Иначе при однопоточном
исполнителе колбэк отмены встанет в очередь за выполняющейся целью и
получит управление через несколько секунд -- при формально корректном коде
и заявленном требовании <200 мс. Это самая дорогая ошибка в этом файле,
и она невидима на глаз.

Колбэк отмены не делает ничего, кроме bump() стока и установки флага.
Ни публикаций, ни логирования на критическом пути: всё это -- на таймере.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import rclpy
from builtin_interfaces.msg import Time as TimeMsg
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll, SayStreamChunk, SpeakingStatus, SystemEvent
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.backends import TtsBackend, make_backend
from guide_robot_voice.lib.chunker import ChunkerConfig, TextChunker
from guide_robot_voice.lib.qos import (
    QOS_CANCEL_ALL,
    QOS_SAY_STREAM,
    QOS_SYSTEM_EVENT,
    QOS_VOICE_SPEAKING,
)
from guide_robot_voice.lib.remote_sink import RemoteSink
from guide_robot_voice.lib.resampler import Resampler, resample_int16
from guide_robot_voice.lib.scheduler import Action, Scheduler, Scope, Utterance
from guide_robot_voice.lib.sink import EpochFencedSink, KeepAliveTone, SoundDeviceEmitter
from guide_robot_voice.lib.text_stream import ClauseFeed, TextStreams


class TtsNode(LifecycleNode):
    """Lifecycle-нода синтеза и воспроизведения речи."""

    def __init__(self) -> None:
        """Объявить параметры. Ресурсы захватываются в on_configure."""
        super().__init__("tts_node")

        self.declare_parameter("backend", "silero")
        self.declare_parameter("model_path", "")
        self.declare_parameter("config_path", "")
        self.declare_parameter("speaker", "xenia")
        self.declare_parameter("silero_sample_rate", 48000)
        self.declare_parameter("speaker_id", 0)
        self.declare_parameter("length_scale", 1.0)
        # Silero: темп (<prosody rate>, "100%" -- как есть), явная пауза между
        # предложениями (0 -- модельная ~400 мс) и обрезка тишины в конце
        # клаузы (-1 -- не трогать). См. lib/backends.build_silero_ssml.
        self.declare_parameter("silero_rate", "100%")
        self.declare_parameter("sentence_pause_ms", 0)
        self.declare_parameter("trailing_silence_ms", -1)
        self.declare_parameter("device", "")
        self.declare_parameter("device_rate", 0)
        self.declare_parameter("block_ms", 20)
        self.declare_parameter("periods", 3)
        self.declare_parameter("channels", 2)
        self.declare_parameter("allow_shared", False)
        self.declare_parameter("sink_backend", "local")
        self.declare_parameter("remote_service_timeout", 3.0)
        self.declare_parameter("max_queue_ms", 600)
        self.declare_parameter("fade_out_ms", 80)
        # keep-alive: инфразвуковой тон вместо нулей в паузах, чтобы USB-кодек
        # с авто-mute не «засыпал» (0.0 -- выключено; см. lib/sink.KeepAliveTone).
        self.declare_parameter("keepalive_dbfs", 0.0)
        self.declare_parameter("keepalive_hz", 20.0)
        self.declare_parameter("min_chars", 40)
        self.declare_parameter("max_clause_chars", 180)
        self.declare_parameter("chars_per_second", 14.0)
        self.declare_parameter("heartbeat_hz", 5.0)
        self.declare_parameter("max_queue", 8)
        self.declare_parameter("warmup_text", "Система готова")
        self.declare_parameter("default_priority", 50)
        # Потоковая Say-цель (stream_id): сколько ждать следующего куска
        # ответа, прежде чем считать реплику законченной без final.
        self.declare_parameter("stream_idle_timeout_s", 10.0)

        self._backend: TtsBackend | None = None
        self._sink: EpochFencedSink | RemoteSink | None = None
        self._chunker: TextChunker | None = None
        self._resampler: Resampler | None = None
        self._scheduler = Scheduler()
        self._scheduler_lock = threading.Lock()
        self._text_streams = TextStreams()

        self._preempted: set[str] = set()
        self._active_goal_id = ""
        self._active_stream_id = ""
        self._active_priority = 0
        self._active_scope = int(Scope.DIALOG)
        self._active_interruptible = True
        self._speaking = False
        self._expected_end = 0.0
        self._stage = "инициализация"
        self._pending_barge_in_latency_ms: float | None = None
        """Выставляется в _on_cancel_all(), публикуется таймером -- не на критическом пути."""

        self._cb_cancel = MutuallyExclusiveCallbackGroup()
        self._cb_action = ReentrantCallbackGroup()
        self._cb_timer = MutuallyExclusiveCallbackGroup()
        self._cb_stream = MutuallyExclusiveCallbackGroup()

        # Эти сущности создаются в on_configure(), а не в __init__().
        # Храним явные None, чтобы частично неудавшийся configure и повторный
        # lifecycle-цикл могли безопасно освободить только уже созданное.
        (
            self._status_pub,
            self._diag_pub,
            self._event_pub,
            self._cancel_sub,
            self._stream_sub,
            self._action_server,
            self._status_timer,
        ) = (None,) * 7

    # -- lifecycle ----------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить модель, открыть устройство, поднять интерфейсы.

        Тело целиком в try. Исключение, вылетевшее из колбэка перехода
        lifecycle, поглощается машиной состояний: наружу приходит только
        "Transitioning failed" без единого слова о причине. Ловить надо
        всё, а не только загрузку модели.
        """
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался на шаге '{self._stage}': {error}")
            self._release_resources()
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        """Собственно конфигурация. Каждый шаг помечается в self._stage."""
        self._stage = "загрузка модели"
        self.get_logger().info("загружаю модель TTS...")
        self._backend = self._build_backend()
        self._backend.load()

        self._stage = "чанкер"
        self._chunker = TextChunker(
            ChunkerConfig(
                min_chars=int(self.get_parameter("min_chars").value),
                max_chars=int(self.get_parameter("max_clause_chars").value),
                chars_per_second=float(self.get_parameter("chars_per_second").value),
            )
        )

        self._stage = "ресемплер"
        # Частота устройства и частота модели совпадают редко: русский голос
        # Piper -- 22050, USB Audio Class обычно только 48000. hw: ничего
        # не конвертирует, поэтому пересчёт делается здесь и явно.
        device_rate = int(self.get_parameter("device_rate").value) or self._backend.sample_rate
        self._resampler = Resampler(self._backend.sample_rate, device_rate)
        if not self._resampler.passthrough:
            engine = "scipy polyphase" if self._resampler.uses_scipy else "линейная интерполяция"
            self.get_logger().info(
                f"ресемплинг {self._backend.sample_rate} -> {device_rate} Гц ({engine})"
            )

        block_ms = int(self.get_parameter("block_ms").value)
        periods = int(self.get_parameter("periods").value)
        device = self.get_parameter("device").value or None
        sink_backend = str(self.get_parameter("sink_backend").value)
        if sink_backend == "xvf3800":
            self._stage = "подключение RemoteSink к xvf3800_audio_node"
            self._sink = RemoteSink(
                self,
                sample_rate=device_rate,
                fade_out_ms=int(self.get_parameter("fade_out_ms").value),
                service_timeout=float(self.get_parameter("remote_service_timeout").value),
            )
        elif sink_backend == "local":
            self._stage = f"открытие устройства вывода ({device or 'по умолчанию'})"
            self.get_logger().info(f"открываю устройство вывода: {device or 'по умолчанию'}")
            emitter = SoundDeviceEmitter(
                sample_rate=device_rate,
                channels=int(self.get_parameter("channels").value),
                block_ms=block_ms,
                buffer_ms=periods * block_ms,
                device=device,
                allow_shared=bool(self.get_parameter("allow_shared").value),
            )
            self._sink = EpochFencedSink(
                emitter,
                sample_rate=device_rate,
                max_queue_ms=int(self.get_parameter("max_queue_ms").value),
                fade_out_ms=int(self.get_parameter("fade_out_ms").value),
            )
        else:
            raise ValueError("sink_backend должен быть local или xvf3800")

        self._stage = "интерфейсы ROS"
        self._scheduler = Scheduler(max_queue=int(self.get_parameter("max_queue").value))
        self._status_pub = self.create_lifecycle_publisher(
            SpeakingStatus, "/voice/speaking", QOS_VOICE_SPEAKING
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )
        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_cancel,
        )
        self._stream_sub = self.create_subscription(
            SayStreamChunk,
            "/speech/say_stream",
            self._on_say_stream,
            QOS_SAY_STREAM,
            callback_group=self._cb_stream,
        )
        self._action_server = ActionServer(
            self,
            Say,
            "say",
            execute_callback=self._execute,
            goal_callback=self._on_goal,
            cancel_callback=self._on_goal_cancel,
            callback_group=self._cb_action,
        )
        heartbeat_hz = float(self.get_parameter("heartbeat_hz").value)
        self._status_timer = self.create_timer(
            1.0 / heartbeat_hz,
            self._publish_status,
            callback_group=self._cb_timer,
        )

        self._stage = "готово"
        self.get_logger().info(
            f"tts_node сконфигурирован: бэкенд={self.get_parameter('backend').value}, "
            f"sink={sink_backend}, "
            f"модель {self._backend.sample_rate} Гц, устройство {device_rate} Гц, "
            f"блок {block_ms} мс, темп {self.get_parameter('silero_rate').value}, "
            f"пауза между предложениями {self.get_parameter('sentence_pause_ms').value} мс"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Открыть поток вывода и разогреть модель."""
        try:
            assert self._sink is not None
            assert self._backend is not None
            self._stage = "запуск вывода"
            self._sink.start()
            self._stage = "разогрев модели"
            started = time.monotonic()
            warmup_text = str(self.get_parameter("warmup_text").value)
            for _ in self._backend.synthesize(warmup_text):
                pass
            self.get_logger().info(f"разогрев занял {(time.monotonic() - started) * 1e3:.0f} мс")
        except Exception as error:
            self.get_logger().error(f"activate не удался на шаге '{self._stage}': {error}")
            return TransitionCallbackReturn.FAILURE
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Заглушить выход. Вызывается при постановке на зарядку."""
        if self._sink is not None:
            self._sink.bump("deactivate")
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить устройство и модель."""
        del state
        self._release_resources()
        return TransitionCallbackReturn.SUCCESS

    def _release_resources(self) -> None:
        """Удалить ROS-интерфейсы и тяжёлые ресурсы текущей конфигурации.

        Простого присваивания нового ActionServer при следующем configure
        недостаточно: старый сервер некоторое время остаётся в DDS-графе и
        две реализации /say могут принять одну цель. Поэтому lifecycle
        cleanup обязан уничтожать сущности явно, до новой конфигурации.
        """
        if self._status_timer is not None:
            self.destroy_timer(self._status_timer)
            self._status_timer = None
        if self._action_server is not None:
            self._action_server.destroy()
            self._action_server = None
        if self._cancel_sub is not None:
            self.destroy_subscription(self._cancel_sub)
            self._cancel_sub = None
        if self._stream_sub is not None:
            self.destroy_subscription(self._stream_sub)
            self._stream_sub = None
        for attribute in ("_status_pub", "_diag_pub", "_event_pub"):
            publisher = getattr(self, attribute)
            if publisher is not None:
                self.destroy_lifecycle_publisher(publisher)
                setattr(self, attribute, None)
        if self._sink is not None:
            self._sink.close()
            self._sink = None
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- отмена ---------------------------------------------------------

    def _on_cancel_all(self, msg: CancelAll) -> None:
        """Аварийная отмена. Критический путь -- держать коротким.

        scheduler.cancel() -- ПЕРВЫМ, под локом: только он знает, задевает
        ли scope этой отмены то, что сейчас реально играет на устройстве.
        sink.bump() рвёт физический вывод безусловно и без него звонить
        нельзя -- если активная реплика пережила scope-фильтр (например,
        SCOPE_DIALOG-ответ ЛЛМ при CancelAll(scope=narration)) или защищена
        interruptible=False, звук трогать нельзя, иначе отмена одного scope
        глушит чужой звук, которого формально не касалась (был баг: ответ
        ЛЛМ обрывался собственным эхом barge-in -- нарратив гасил barge-in'ом
        весь вывод целиком, а не только свой scope).

        Не сверяется с msg.epoch: bump() идемпотентен на пустом стоке,
        поэтому сравнивать "свежее или нет" незачем -- это же исключает
        дефект, описанный в design §0.1 (гонка нескольких издателей
        CancelAll с независимыми счётчиками).
        """
        if self._sink is None:
            return

        with self._scheduler_lock:
            dropped_active, dropped_queue = self._scheduler.cancel(Scope(msg.scope), msg.reason)
            if dropped_active is not None:
                self._preempted.add(dropped_active.goal_id)
            for utterance in dropped_queue:
                self._preempted.add(utterance.goal_id)

        if dropped_active is None:
            # Активная реплика (другого scope либо interruptible=False)
            # пережила отмену -- физический вывод не трогаем.
            return

        self._sink.bump(msg.reason)
        if self._resampler is not None:
            self._resampler.reset()

        self._speaking = False

        if msg.reason == CancelAll.REASON_BARGE_IN:
            # Только арифметика -- публикация SystemEvent идёт с таймера
            # _publish_status, не отсюда (design: "ни публикаций на
            # критическом пути"). msg.stamp -- момент начала речи
            # посетителя (design §4), не момент публикации CancelAll.
            onset_ns = msg.stamp.sec * 1_000_000_000 + msg.stamp.nanosec
            now_ns = self.get_clock().now().nanoseconds
            self._pending_barge_in_latency_ms = (now_ns - onset_ns) / 1e6

    def _on_say_stream(self, msg: SayStreamChunk) -> None:
        """Продолжение потоковой Say-цели; cancel рвёт звук сразу, как goal cancel."""
        self._text_streams.feed(msg.stream_id, msg.text, final=msg.final, cancel=msg.cancel)
        if not msg.cancel or self._sink is None:
            return
        with self._scheduler_lock:
            active_id = self._active_goal_id
            is_active = bool(active_id) and self._active_stream_id == msg.stream_id
        if is_active:
            self._sink.bump("stream_cancel")
            if self._resampler is not None:
                self._resampler.reset()

    def _on_goal_cancel(self, goal_handle: object) -> CancelResponse:
        """Немедленно fenced-нуть PCM отменяемой активной Say-цели."""
        if self._sink is not None:
            goal_id = bytes(goal_handle.goal_id.uuid).hex()  # type: ignore[attr-defined]
            with self._scheduler_lock:
                active = self._scheduler.active
                is_active = active is not None and active.goal_id == goal_id
            if is_active:
                self._sink.bump("goal_cancel")
                if self._resampler is not None:
                    self._resampler.reset()
        return CancelResponse.ACCEPT

    # -- приём целей ------------------------------------------------------

    def _on_goal(self, goal_request: Say.Goal) -> GoalResponse:
        """Отбросить пустой текст до постановки в очередь."""
        if not goal_request.text.strip():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute(self, goal_handle: object) -> Say.Result:
        """Синтезировать и воспроизвести текст цели."""
        assert self._sink is not None
        assert self._chunker is not None
        assert self._backend is not None

        request: Say.Goal = goal_handle.request  # type: ignore[attr-defined]
        goal_id = bytes(goal_handle.goal_id.uuid).hex()  # type: ignore[attr-defined]

        priority = int(request.priority) or int(self.get_parameter("default_priority").value)
        utterance = Utterance(
            goal_id=goal_id,
            text=request.text,
            priority=priority,
            scope=Scope(int(request.scope)),
            voice=request.voice,
            interruptible=bool(request.interruptible),
            max_duration=float(request.max_duration),
            seq=self._scheduler.next_seq(),
        )

        with self._scheduler_lock:
            decision = self._scheduler.submit(utterance)
            if decision.action is Action.PREEMPT and decision.victim is not None:
                self._preempted.add(decision.victim.goal_id)

        if decision.action is Action.REJECT:
            goal_handle.abort()  # type: ignore[attr-defined]
            return Say.Result(status=Say.Result.STATUS_REJECTED, message="queue_full")

        if decision.action is Action.PREEMPT:
            # Вытеснение рвёт аудио предыдущей цели немедленно.
            self._sink.bump("preempted_by_higher_priority")

        stream_id = str(request.stream_id)
        if decision.action is Action.QUEUE and not self._wait_for_turn(goal_id, goal_handle):
            self._text_streams.release(stream_id)
            if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                goal_handle.canceled()  # type: ignore[attr-defined]
            else:
                goal_handle.abort()  # type: ignore[attr-defined]
            return self._finish(goal_id, Say.Result(status=Say.Result.STATUS_CANCELLED))

        try:
            return self._speak(goal_handle, utterance, stream_id)
        finally:
            self._text_streams.release(stream_id)

    def _wait_for_turn(self, goal_id: str, goal_handle: object) -> bool:
        """Дождаться, пока планировщик сделает цель активной."""
        while rclpy.ok():
            with self._scheduler_lock:
                active = self._scheduler.active
                preempted = goal_id in self._preempted
            if preempted or goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                return False
            if active is not None and active.goal_id == goal_id:
                return True
            time.sleep(0.01)
        return False

    # -- воспроизведение ----------------------------------------------------

    def _speak(  # noqa: PLR0912, PLR0915 -- линейная orchestration Say lifecycle
        self, goal_handle: object, utterance: Utterance, stream_id: str = ""
    ) -> Say.Result:
        """Основной цикл: клауза -> синтез -> сток, с проверкой epoch."""
        assert self._sink is not None
        assert self._chunker is not None
        assert self._backend is not None

        clauses = ClauseFeed(
            self._chunker,
            utterance.text,
            streams=self._text_streams,
            stream_id=stream_id,
            should_stop=lambda: (
                utterance.goal_id in self._preempted or goal_handle.is_cancel_requested  # type: ignore[attr-defined]
            ),
            idle_timeout_s=float(self.get_parameter("stream_idle_timeout_s").value),
        )
        try:
            epoch = self._sink.begin(utterance.goal_id)
        except Exception as error:
            self.get_logger().error(f"не удалось открыть playback stream: {error}")
            goal_handle.abort()  # type: ignore[attr-defined]
            return self._finish(
                utterance.goal_id,
                Say.Result(
                    status=Say.Result.STATUS_FAILED,
                    message=f"playback_begin_error: {error}"[:200],
                ),
            )
        started = time.monotonic()
        spoken_chars = 0
        status = Say.Result.STATUS_COMPLETED
        message = ""

        self._mark_active(utterance, started)
        self._active_stream_id = stream_id
        self._publish_status()

        for clause in clauses:
            if utterance.goal_id in self._preempted:
                status, message = Say.Result.STATUS_PREEMPTED, "cancel_all"
                break
            if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                status, message = Say.Result.STATUS_CANCELLED, "goal_cancel"
                break
            if utterance.max_duration > 0 and time.monotonic() - started > utterance.max_duration:
                status, message = Say.Result.STATUS_PREEMPTED, "max_duration"
                self._sink.bump("max_duration")
                if self._resampler is not None:
                    self._resampler.reset()
                break

            feedback = Say.Feedback(
                clause_index=clause.index,
                clause_count=clauses.count,
                progress=spoken_chars / max(1, len(clauses.text)),
                current_clause=clause.text,
            )
            goal_handle.publish_feedback(feedback)  # type: ignore[attr-defined]

            try:
                pushed = self._push_clause(clause.text, utterance.voice, epoch)
            except Exception as error:
                self.get_logger().error(f"синтез клаузы не удался: {error}")
                self._sink.bump("synthesis_error")
                status = Say.Result.STATUS_FAILED
                message = f"synthesis_error: {error}"[:200]
                break
            if not pushed:
                if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                    status, message = Say.Result.STATUS_CANCELLED, "goal_cancel"
                else:
                    status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"
                break

            # Для RemoteSink это аппаратный checkpoint: все сэмплы клаузы
            # уже прошли playback timeline XVF3800. При cancel/fence epoch
            # меняется и ожидание сразу возвращает False, поэтому
            # narration_server не пропустит фактически не прозвучавший текст.
            if not self._sink.wait_presented(epoch):
                if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                    status, message = Say.Result.STATUS_CANCELLED, "goal_cancel"
                else:
                    status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"
                break

            # Символы засчитываются только за подтверждённую целую клаузу.
            # Половина клаузы в очереди -- это не "прозвучало", и завышать
            # spoken_chars нельзя: narration_server возобновит монолог
            # с пропуском куска текста.
            spoken_chars = clause.char_end

        if status == Say.Result.STATUS_COMPLETED and clauses.streaming:
            # Потоковая цель выходит из цикла и по отмене, пока ждёт кусок.
            if utterance.goal_id in self._preempted:
                status, message = Say.Result.STATUS_PREEMPTED, "cancel_all"
            elif goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                status, message = Say.Result.STATUS_CANCELLED, "goal_cancel"
            elif clauses.cancelled:
                status, message = Say.Result.STATUS_PREEMPTED, "stream_cancel"
            elif clauses.timed_out:
                self.get_logger().warning(
                    f"stream {stream_id}: нет final, реплика закрыта по таймауту"
                )

        if status == Say.Result.STATUS_COMPLETED and not self._flush_resampler_tail(epoch):
            status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"

        if status == Say.Result.STATUS_COMPLETED and not self._sink.wait_idle(epoch):
            status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"

        result = Say.Result(
            status=status,
            spoken_text=clauses.text[:spoken_chars],
            spoken_chars=spoken_chars,
            spoken_duration=float(time.monotonic() - started),
            message=message,
        )

        if status == Say.Result.STATUS_COMPLETED:
            goal_handle.succeed()  # type: ignore[attr-defined]
        elif status == Say.Result.STATUS_CANCELLED:
            goal_handle.canceled()  # type: ignore[attr-defined]
        else:
            goal_handle.abort()  # type: ignore[attr-defined]

        return self._finish(utterance.goal_id, result)

    def _flush_resampler_tail(self, epoch: int) -> bool:
        """Слить хвост фильтра `soxr` на ЧИСТОМ конце реплики. False -- нас отменили.

        stage3 B1: `soxr` держит хвост фильтра во внутреннем буфере между
        вызовами `process()` -- без явного `flush()` здесь теряются
        последние ~десятки мс речи (измерено: до ~33мс на 22050->48000).
        Звать только на happy path -- ранний break (PREEMPTED/CANCELLED/
        FAILED) уже сбросил буфер через `_resampler.reset()` внутри
        `_push_clause()`, фраза оборвана и флешить остаток нечего и не
        нужно.
        """
        assert self._resampler is not None
        assert self._sink is not None
        tail = self._resampler.flush()
        if not tail.size:
            return True
        return self._sink.submit(epoch, tail)

    def _push_clause(self, text: str, voice: str, epoch: int) -> bool:
        """Синтезировать клаузу и подать в сток. False -- нас отменили.

        Бэкенд (в частности, Piper) изредка бросает исключение прямо из
        onnxruntime -- наблюдалось на реальном железе как случайный сбой
        стохастического duration predictor (не каждый вызов, один и тот же
        текст может и упасть, и синтезироваться нормально). Раз до сброса
        стока ничего ещё не поставлено, один повтор безопасен и обычно
        достаточен. Если часть клаузы уже ушла в сток -- повторять нельзя:
        это удвоит звук. В этом случае и после исчерпания попыток
        исключение прокидывается наверх, в _speak(), как настоящий сбой
        (STATUS_FAILED), а не отмена.
        """
        assert self._sink is not None
        assert self._backend is not None
        assert self._resampler is not None
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            pushed_any = False
            remote_source_blocks: list[np.ndarray] = []
            try:
                for block in self._backend.synthesize(text, voice):
                    if self._sink.epoch != epoch:
                        self._resampler.reset()
                        return False
                    if self._sink.prefers_clause_batches:
                        # Silero уже вернул всю фразу до первого yield, а
                        # RemoteSink всё равно посылает её крупными блоками.
                        # Собираем исходную клаузу и ресемплируем одним
                        # полифазным вызовом: без soxr это исключает стыки
                        # фильтра на каждые 20 мс.
                        remote_source_blocks.append(block)
                        continue
                    converted = self._resampler.process(block)
                    if not converted.size:
                        continue
                    if not self._sink.submit(epoch, converted):
                        self._resampler.reset()
                        return False
                    pushed_any = True
                if remote_source_blocks:
                    # Один PlayPcm на каждый разрешённый аппаратной нодой
                    # блок, а не action round-trip на каждые 20 мс TTS.
                    # RemoteSink сам режет массив по max_pcm_samples.
                    source_pcm = np.concatenate(remote_source_blocks)
                    clause_pcm = resample_int16(
                        source_pcm,
                        self._backend.sample_rate,
                        self._resampler.target_rate,
                    )
                    if not self._sink.submit(epoch, clause_pcm):
                        self._resampler.reset()
                        return False
                    pushed_any = True
            except Exception:
                self._resampler.reset()
                if pushed_any or attempt >= max_attempts:
                    raise
                self.get_logger().warning(
                    f"синтез клаузы не удался до вывода звука "
                    f"(попытка {attempt}/{max_attempts}), повторяю"
                )
                continue
            return True
        raise AssertionError("unreachable: max_attempts >= 1")

    def _finish(self, goal_id: str, result: Say.Result) -> Say.Result:
        """Снять цель с планировщика и обновить статус."""
        with self._scheduler_lock:
            self._scheduler.finish(goal_id)
            self._preempted.discard(goal_id)
            still_active = self._scheduler.active
        if still_active is None or still_active.goal_id != self._active_goal_id:
            self._speaking = False
            self._active_goal_id = ""
            self._active_stream_id = ""
        self._publish_status()
        return result

    def _mark_active(self, utterance: Utterance, started: float) -> None:
        """Записать поля активного высказывания -- вынесено из `_speak()` (PLR0915)."""
        self._active_goal_id = utterance.goal_id
        self._active_priority = utterance.priority
        self._active_scope = int(utterance.scope)
        self._active_interruptible = utterance.interruptible
        self._speaking = True
        self._expected_end = started + self._chunker.config.estimate_seconds(utterance.text)  # type: ignore[union-attr]

    # -- телеметрия -----------------------------------------------------

    def _publish_status(self) -> None:
        """Опубликовать SpeakingStatus и диагностику."""
        if self._sink is None:
            return
        now = self.get_clock().now()
        status = SpeakingStatus()
        status.stamp = now.to_msg()
        status.speaking = self._speaking and (
            self._sink.is_playing if self._sink.reports_hardware_state else True
        )
        status.epoch = self._sink.epoch
        status.goal_id = self._active_goal_id
        status.priority = self._active_priority
        status.scope = self._active_scope
        status.interruptible = self._active_interruptible
        status.expected_end = self._to_time_msg(self._expected_end)
        self._status_pub.publish(status)

        metrics = self._sink.metrics
        diag = DiagnosticArray()
        diag.header.stamp = status.stamp
        entry = DiagnosticStatus(
            name="voice/tts",
            hardware_id="tts_node",
            level=DiagnosticStatus.OK,
            message="speaking" if status.speaking else "idle",
            values=[
                KeyValue(key="epoch", value=str(self._sink.epoch)),
                KeyValue(key="t_stop_ms", value=f"{metrics.t_stop_ms:.2f}"),
                KeyValue(key="last_cancel_reason", value=metrics.reason),
                KeyValue(key="dropped_frames", value=str(metrics.dropped_frames)),
                KeyValue(key="queue_seconds", value=f"{self._sink.pending_seconds():.3f}"),
            ],
        )
        diag.status.append(entry)
        self._diag_pub.publish(diag)

        if self._pending_barge_in_latency_ms is not None:
            latency_ms = self._pending_barge_in_latency_ms
            self._pending_barge_in_latency_ms = None
            event = SystemEvent(
                id="voice.barge_in_latency",
                severity=SystemEvent.INFO,
                detail=f"latency_ms={latency_ms:.1f}",
            )
            event.header.stamp = status.stamp
            self._event_pub.publish(event)
            self.get_logger().info(f"barge-in latency: {latency_ms:.1f} мс")

    def _to_time_msg(self, monotonic_deadline: float) -> TimeMsg:
        """Перевести monotonic-дедлайн в ROS-время."""
        remaining = max(0.0, monotonic_deadline - time.monotonic())
        now = self.get_clock().now().nanoseconds
        target = now + int(remaining * 1e9)
        return TimeMsg(sec=int(target // 10**9), nanosec=int(target % 10**9))

    # -- сборка бэкенда -----------------------------------------------------

    def _build_backend(self) -> TtsBackend:
        """Собрать бэкенд по параметрам.

        backend по умолчанию -- silero (v5 xenia). "piper" -- запасной
        ONNX-голос. "null" -- тон без модели, для CI и измерения t_stop.
        """
        kind = str(self.get_parameter("backend").value)
        if kind == "null":
            return make_backend("null")
        if kind == "piper":
            return make_backend(
                "piper",
                model_path=str(self.get_parameter("model_path").value),
                config_path=str(self.get_parameter("config_path").value),
                speaker_id=int(self.get_parameter("speaker_id").value),
                length_scale=float(self.get_parameter("length_scale").value),
            )
        if kind == "silero":
            return make_backend(
                "silero",
                model_path=str(self.get_parameter("model_path").value),
                speaker=str(self.get_parameter("speaker").value),
                sample_rate=int(self.get_parameter("silero_sample_rate").value),
                block_ms=int(self.get_parameter("block_ms").value),
                rate=str(self.get_parameter("silero_rate").value),
                sentence_pause_ms=int(self.get_parameter("sentence_pause_ms").value),
                trailing_silence_ms=int(self.get_parameter("trailing_silence_ms").value),
            )
        raise ValueError(f"неизвестный бэкенд: {kind!r}, ожидается 'silero', 'piper' или 'null'")


def main(args: list[str] | None = None) -> None:
    """Точка входа. MultiThreadedExecutor обязателен, см. шапку модуля."""
    rclpy.init(args=args)
    node = TtsNode()
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
