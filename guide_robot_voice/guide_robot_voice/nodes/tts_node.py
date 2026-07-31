"""Нода синтеза речи.

Собирает вместе четыре независимо тестируемых куска: чанкер, планировщик,
бэкенд и epoch-fenced сток. Сама нода отвечает только за ROS-обвязку
и за то, чтобы отмена не попала на медленный путь.

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

import rclpy
from builtin_interfaces.msg import Time as TimeMsg
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll, SpeakingStatus
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from guide_robot_voice.audio.resample import Resampler
from guide_robot_voice.audio.sink import EpochFencedSink, SoundDeviceEmitter
from guide_robot_voice.tts.backends import make_backend
from guide_robot_voice.tts.chunker import ChunkerConfig, TextChunker
from guide_robot_voice.tts.scheduler import Action, Scheduler, Scope, Utterance

QOS_COMMAND = QoSProfile(
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

QOS_STATUS = QoSProfile(
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)


class TtsNode(LifecycleNode):
    """Lifecycle-нода синтеза и воспроизведения речи."""

    def __init__(self) -> None:
        """Объявить параметры. Ресурсы захватываются в on_configure."""
        super().__init__("tts_node")

        self.declare_parameter("backend", "null")
        self.declare_parameter("model_path", "")
        self.declare_parameter("config_path", "")
        self.declare_parameter("voice", "")
        self.declare_parameter("device", "")
        self.declare_parameter("block_ms", 20)
        self.declare_parameter("buffer_ms", 80)
        self.declare_parameter("channels", 2)
        self.declare_parameter("device_rate", 0)
        self.declare_parameter("allow_shared", False)
        self.declare_parameter("max_queue_ms", 600)
        self.declare_parameter("min_chars", 40)
        self.declare_parameter("max_chars", 160)
        self.declare_parameter("chars_per_second", 14.0)
        self.declare_parameter("status_period", 0.2)
        self.declare_parameter("max_pending_goals", 8)

        self._backend = None
        self._sink: EpochFencedSink | None = None
        self._chunker: TextChunker | None = None
        self._scheduler = Scheduler()
        self._scheduler_lock = threading.Lock()

        self._preempted: set[str] = set()
        self._active_goal_id = ""
        self._active_priority = 0
        self._active_scope = int(Scope.DIALOG)
        self._speaking = False
        self._expected_end = 0.0
        self._last_t_stop_ms = 0.0
        self._stage = "инициализация"

        self._cb_cancel = MutuallyExclusiveCallbackGroup()
        self._cb_action = ReentrantCallbackGroup()
        self._cb_timer = MutuallyExclusiveCallbackGroup()

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
                max_chars=int(self.get_parameter("max_chars").value),
                chars_per_second=float(self.get_parameter("chars_per_second").value),
            )
        )

        self._stage = "ресемплер"
        # Частота устройства и частота модели совпадают редко: русские голоса
        # Piper -- 22050, USB Audio Class обычно только 48000. hw: ничего
        # не конвертирует, поэтому пересчёт делается здесь и явно.
        device_rate = int(self.get_parameter("device_rate").value) or self._backend.sample_rate
        self._resampler = Resampler(self._backend.sample_rate, device_rate)
        if not self._resampler.passthrough:
            engine = "scipy polyphase" if self._resampler.uses_scipy else "линейная интерполяция"
            self.get_logger().info(
                f"ресемплинг {self._backend.sample_rate} -> {device_rate} Гц ({engine})"
            )

        device = self.get_parameter("device").value or None
        self._stage = f"открытие устройства вывода ({device or 'по умолчанию'})"
        self.get_logger().info(f"открываю устройство вывода: {device or 'по умолчанию'}")
        emitter = SoundDeviceEmitter(
            sample_rate=device_rate,
            channels=int(self.get_parameter("channels").value),
            block_ms=int(self.get_parameter("block_ms").value),
            buffer_ms=int(self.get_parameter("buffer_ms").value),
            device=device,
            allow_shared=bool(self.get_parameter("allow_shared").value),
        )
        self._sink = EpochFencedSink(
            emitter,
            sample_rate=device_rate,
            max_queue_ms=int(self.get_parameter("max_queue_ms").value),
        )

        self._stage = "интерфейсы ROS"
        self._scheduler = Scheduler(max_queue=int(self.get_parameter("max_pending_goals").value))
        self._status_pub = self.create_lifecycle_publisher(
            SpeakingStatus, "/voice/is_speaking", QOS_STATUS
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)
        self._cancel_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_COMMAND,
            callback_group=self._cb_cancel,
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
        self._status_timer = self.create_timer(
            float(self.get_parameter("status_period").value),
            self._publish_status,
            callback_group=self._cb_timer,
        )

        self._stage = "готово"
        self.get_logger().info(
            f"tts_node сконфигурирован: бэкенд={self.get_parameter('backend').value}, "
            f"модель {self._backend.sample_rate} Гц, устройство {device_rate} Гц, "
            f"блок {self.get_parameter('block_ms').value} мс"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Открыть поток вывода и разогреть модель."""
        try:
            assert self._sink is not None and self._backend is not None
            self._stage = "запуск вывода"
            self._sink.start()
            self._stage = "разогрев модели"
            started = time.monotonic()
            self._backend.warmup()
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
        if self._sink is not None:
            self._sink.close()
            self._sink = None
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- отмена -------------------------------------------------------------

    def _on_cancel_all(self, msg: CancelAll) -> None:
        """Аварийная отмена. Критический путь -- держать коротким.

        Здесь нет ни публикаций, ни форматирования строк для лога:
        всё, что можно отложить, откладывается на таймер статуса.
        """
        if self._sink is None:
            return

        epoch = self._sink.bump(msg.reason)
        self._resampler.reset()

        with self._scheduler_lock:
            dropped_active, dropped_queue = self._scheduler.cancel(Scope(msg.scope))
            if dropped_active is not None:
                self._preempted.add(dropped_active.goal_id)
            for utterance in dropped_queue:
                self._preempted.add(utterance.goal_id)

        self._last_t_stop_ms = self._sink.metrics.t_stop_ms
        self._speaking = False
        del epoch

    def _on_goal_cancel(self, goal_handle: object) -> CancelResponse:
        """Штатная отмена одной цели: ждёт границы клаузы."""
        del goal_handle
        return CancelResponse.ACCEPT

    # -- приём целей --------------------------------------------------------

    def _on_goal(self, goal_request: Say.Goal) -> GoalResponse:
        """Отбросить пустой текст до постановки в очередь."""
        if not goal_request.text.strip():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute(self, goal_handle: object) -> Say.Result:
        """Синтезировать и воспроизвести текст цели."""
        assert self._sink is not None and self._chunker is not None
        assert self._backend is not None

        request: Say.Goal = goal_handle.request  # type: ignore[attr-defined]
        goal_id = bytes(goal_handle.goal_id.uuid).hex()  # type: ignore[attr-defined]

        utterance = Utterance(
            goal_id=goal_id,
            text=request.text,
            priority=int(request.priority),
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

        if decision.action is Action.QUEUE and not self._wait_for_turn(goal_id, goal_handle):
            return self._finish(goal_id, Say.Result(status=Say.Result.STATUS_CANCELLED))

        return self._speak(goal_handle, utterance)

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

    def _speak(self, goal_handle: object, utterance: Utterance) -> Say.Result:
        """Основной цикл: клауза -> синтез -> сток, с проверкой epoch."""
        assert self._sink is not None and self._chunker is not None
        assert self._backend is not None

        clauses = self._chunker.split(utterance.text)
        epoch = self._sink.epoch
        started = time.monotonic()
        spoken_chars = 0
        status = Say.Result.STATUS_COMPLETED
        message = ""

        self._active_goal_id = utterance.goal_id
        self._active_priority = utterance.priority
        self._active_scope = int(utterance.scope)
        self._speaking = True
        self._expected_end = started + self._chunker.config.estimate_seconds(utterance.text)
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
                break

            feedback = Say.Feedback(
                clause_index=clause.index,
                clause_count=len(clauses),
                progress=spoken_chars / max(1, len(utterance.text)),
                current_clause=clause.text,
            )
            goal_handle.publish_feedback(feedback)  # type: ignore[attr-defined]

            if not self._push_clause(clause.text, utterance.voice, epoch):
                status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"
                break

            # Символы засчитываются только за полностью поставленную клаузу.
            # Половина клаузы в очереди -- это не "прозвучало", и завышать
            # spoken_chars нельзя: narration_server возобновит монолог
            # с пропуском куска текста.
            spoken_chars = clause.char_end

        if status == Say.Result.STATUS_COMPLETED and not self._sink.wait_idle(epoch):
            status, message = Say.Result.STATUS_PREEMPTED, "epoch_bumped"

        result = Say.Result(
            status=status,
            spoken_text=utterance.text[:spoken_chars],
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

    def _push_clause(self, text: str, voice: str, epoch: int) -> bool:
        """Синтезировать клаузу и подать в сток. False -- нас отменили."""
        assert self._sink is not None and self._backend is not None
        for block in self._backend.synthesize(text, voice):
            converted = self._resampler.process(block)
            if converted.size and not self._sink.submit(epoch, converted):
                self._resampler.reset()
                return False
        return True

    def _finish(self, goal_id: str, result: Say.Result) -> Say.Result:
        """Снять цель с планировщика и обновить статус."""
        with self._scheduler_lock:
            self._scheduler.finish(goal_id)
            self._preempted.discard(goal_id)
            still_active = self._scheduler.active
        if still_active is None or still_active.goal_id != self._active_goal_id:
            self._speaking = False
            self._active_goal_id = ""
        self._publish_status()
        return result

    # -- телеметрия ---------------------------------------------------------

    def _publish_status(self) -> None:
        """Опубликовать SpeakingStatus и диагностику."""
        if self._sink is None:
            return
        now = self.get_clock().now()
        status = SpeakingStatus()
        status.stamp = now.to_msg()
        status.speaking = self._speaking
        status.epoch = self._sink.epoch
        status.goal_id = self._active_goal_id
        status.priority = self._active_priority
        status.scope = self._active_scope
        status.expected_end = self._to_time_msg(self._expected_end)
        self._status_pub.publish(status)

        metrics = self._sink.metrics
        diag = DiagnosticArray()
        diag.header.stamp = status.stamp
        entry = DiagnosticStatus(
            name="voice/tts",
            hardware_id="tts_node",
            level=DiagnosticStatus.OK,
            message="speaking" if self._speaking else "idle",
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

    def _to_time_msg(self, monotonic_deadline: float) -> TimeMsg:
        """Перевести monotonic-дедлайн в ROS-время."""
        remaining = max(0.0, monotonic_deadline - time.monotonic())
        now = self.get_clock().now().nanoseconds
        target = now + int(remaining * 1e9)
        return TimeMsg(sec=int(target // 10**9), nanosec=int(target % 10**9))

    # -- сборка бэкенда -----------------------------------------------------

    def _build_backend(self) -> object:
        """Собрать бэкенд по параметрам."""
        kind = str(self.get_parameter("backend").value)
        if kind == "null":
            return make_backend("null")
        if kind == "piper":
            return make_backend(
                "piper",
                model_path=str(self.get_parameter("model_path").value),
                config_path=str(self.get_parameter("config_path").value),
            )
        if kind == "silero":
            return make_backend(
                "silero",
                model_path=str(self.get_parameter("model_path").value),
                speaker=str(self.get_parameter("voice").value) or "xenia",
            )
        raise ValueError(f"неизвестный бэкенд: {kind!r}")


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
