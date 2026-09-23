"""PCM-выход TTS через отдельный ROS 2 audio owner.

`RemoteSink` сохраняет маленький интерфейс `EpochFencedSink`, но не открывает
PortAudio/ALSA. Физическим устройством единолично владеет
`xvf3800_audio_node`; этот класс только открывает логический stream, передаёт
mono PCM и ждёт подтверждённого аппаратной нодой прогресса.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING

import numpy as np
from guide_robot_msgs.action import PlayPcm
from guide_robot_msgs.msg import PlaybackState
from guide_robot_msgs.srv import BeginPlayback, FencePlayback
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from guide_robot_voice.lib.sink import SinkFailureError, StopMetrics

if TYPE_CHECKING:
    from rclpy.lifecycle import LifecycleNode


QOS_PLAYBACK_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)


class RemoteSink:
    """Адаптер `tts_node` к playback-интерфейсу `xvf3800_audio_node`."""

    reports_hardware_state = True
    prefers_clause_batches = True

    def __init__(
        self,
        node: LifecycleNode,
        sample_rate: int,
        fade_out_ms: int = 20,
        service_timeout: float = 3.0,
    ) -> None:
        """Создать ROS-клиентов; соединение проверяется в :meth:`start`."""
        self._node = node
        self._sample_rate = sample_rate
        self._fade_out_ms = max(0, fade_out_ms)
        self._service_timeout = service_timeout
        self._callbacks = ReentrantCallbackGroup()
        self._begin_client = node.create_client(
            BeginPlayback, "/audio/begin_playback", callback_group=self._callbacks
        )
        self._fence_client = node.create_client(
            FencePlayback, "/audio/fence_playback", callback_group=self._callbacks
        )
        self._play_client = ActionClient(
            node, PlayPcm, "/audio/play_pcm", callback_group=self._callbacks
        )
        self._state_sub = node.create_subscription(
            PlaybackState,
            "/audio/playback_state",
            self._on_state,
            QOS_PLAYBACK_STATE,
            callback_group=self._callbacks,
        )

        self._cv = threading.Condition()
        self._opened = False
        self._closed = False
        self._device_session_id = ""
        self._goal_id = ""
        self._stream_id = 0
        self._epoch = 0
        self._max_pcm_samples = 0
        self._submitted_samples = 0
        self._clause_index = 0
        self._latest_state: PlaybackState | None = None
        self._failure: BaseException | None = None
        self._metrics = StopMetrics()

    def start(self) -> None:
        """Убедиться, что активный audio owner предоставляет весь протокол."""
        if self._opened:
            return
        timeout = self._service_timeout
        if not self._begin_client.wait_for_service(timeout_sec=timeout):
            raise SinkFailureError("сервис /audio/begin_playback недоступен")
        if not self._fence_client.wait_for_service(timeout_sec=timeout):
            raise SinkFailureError("сервис /audio/fence_playback недоступен")
        if not self._play_client.wait_for_server(timeout_sec=timeout):
            raise SinkFailureError("action /audio/play_pcm недоступен")
        self._closed = False
        self._opened = True

    def close(self) -> None:
        """Закрыть текущий stream; ALSA остаётся у audio owner."""
        if self._opened and self._stream_id:
            try:
                self.bump("deactivate", fade_ms=0)
            except Exception as error:  # cleanup обязан продолжиться
                self._failure = error
        with self._cv:
            self._closed = True
            self._opened = False
            self._cv.notify_all()

    def begin(self, goal_id: str) -> int:
        """Открыть новый fenced-stream для одной активной Say-цели."""
        if not self._opened:
            raise SinkFailureError("RemoteSink не запущен")
        request = BeginPlayback.Request()
        request.goal_id = goal_id
        request.expected_device_session_id = self._device_session_id
        request.admission_token = ""
        request.sample_rate = self._sample_rate
        request.channels = 1
        response = self._call(self._begin_client.call_async(request), "BeginPlayback")
        if not response.accepted:
            raise SinkFailureError(f"BeginPlayback отклонён: {response.reason}")

        with self._cv:
            self._device_session_id = response.device_session_id
            self._goal_id = goal_id
            self._stream_id = int(response.stream_id)
            self._epoch = int(response.generation)
            self._max_pcm_samples = int(response.max_pcm_samples)
            self._submitted_samples = 0
            self._clause_index = 0
            self._latest_state = None
            self._cv.notify_all()
            return self._epoch

    @property
    def epoch(self) -> int:
        """Текущее поколение аппаратного stream."""
        with self._cv:
            return self._epoch

    @property
    def metrics(self) -> StopMetrics:
        """Телеметрия последнего FencePlayback."""
        with self._cv:
            return self._metrics

    @property
    def failure(self) -> BaseException | None:
        """Последняя ошибка транспорта."""
        with self._cv:
            return self._failure

    @property
    def is_playing(self) -> bool:
        """True, только когда audio owner подтвердил физический прогресс."""
        with self._cv:
            state = self._matching_state_locked()
            return state is not None and state.state in (
                PlaybackState.STATE_PLAYING,
                PlaybackState.STATE_DRAINING,
            )

    @property
    def underflows(self) -> int:
        """Поле совместимости; XRUN будет публиковать audio owner."""
        return 0

    def raise_if_failed(self) -> None:
        """Поднять последнюю ошибку транспорта в вызывающем потоке."""
        failure = self.failure
        if failure is not None:
            raise SinkFailureError("remote playback отказал") from failure

    def pending_seconds(self) -> float:
        """Текущая глубина очереди audio owner."""
        with self._cv:
            state = self._matching_state_locked()
            return 0.0 if state is None else state.buffered_samples / self._sample_rate

    def played_seconds(self) -> float:
        """Подтверждённая оценка фактически представленного PCM."""
        with self._cv:
            state = self._matching_state_locked()
            return 0.0 if state is None else state.presented_samples_estimate / self._sample_rate

    def submit(self, epoch: int, pcm: np.ndarray, timeout: float = 30.0) -> bool:
        """Передать mono int16 PCM блоками допустимого размера."""
        if self.failure is not None:
            return False
        data = np.asarray(pcm, dtype=np.int16).reshape(-1)
        if data.size == 0:
            return True
        with self._cv:
            if self._closed or epoch != self._epoch or not self._stream_id:
                return False
            maximum = self._max_pcm_samples
        if maximum <= 0:
            return False

        deadline = time.monotonic() + timeout
        for offset in range(0, int(data.size), maximum):
            remaining = deadline - time.monotonic()
            block = data[offset : offset + maximum]
            if remaining <= 0 or not self._submit_block(epoch, block, remaining):
                return False
        return True

    def _submit_block(self, epoch: int, pcm: np.ndarray, timeout: float) -> bool:
        with self._cv:
            if epoch != self._epoch or self._closed:
                return False
            goal = PlayPcm.Goal()
            goal.device_session_id = self._device_session_id
            goal.stream_id = self._stream_id
            goal.generation = self._epoch
            goal.clause_index = self._clause_index
            goal.sample_rate = self._sample_rate
            goal.channels = 1
            goal.pcm = pcm.tolist()
            self._clause_index += 1

        try:
            goal_handle = self._wait_future(
                self._play_client.send_goal_async(goal), timeout, "PlayPcm goal"
            )
            if not goal_handle.accepted:
                return False
            wrapped = self._wait_future(goal_handle.get_result_async(), timeout, "PlayPcm result")
            if wrapped.result.status != PlayPcm.Result.STATUS_ACCEPTED:
                return False
            with self._cv:
                if epoch != self._epoch or self._closed:
                    return False
                self._submitted_samples = max(
                    self._submitted_samples, int(wrapped.result.submitted_samples)
                )
                self._cv.notify_all()
            return True
        except Exception as error:
            with self._cv:
                self._failure = error
                self._cv.notify_all()
            return False

    def bump(self, reason: str = "", *, fade_ms: int | None = None) -> int:
        """Синхронно fenced-нуть stream, чтобы старый PCM больше не принимался."""
        with self._cv:
            if not self._stream_id:
                self._epoch += 1
                return self._epoch
            request = FencePlayback.Request()
            request.request_id = uuid.uuid4().hex
            request.device_session_id = self._device_session_id
            request.stream_id = self._stream_id
            request.expected_generation = self._epoch
            request.reason = reason
            request.fade_ms = self._fade_out_ms if fade_ms is None else max(0, fade_ms)

        requested_at = time.monotonic()
        try:
            response = self._call(self._fence_client.call_async(request), "FencePlayback")
            if not response.accepted:
                raise SinkFailureError(f"FencePlayback отклонён: {response.reason}")
            resulting_epoch = int(response.resulting_generation)
        except Exception as error:
            with self._cv:
                # Локальный epoch всё равно закрывается: TTS обязан прекратить
                # отправку, даже если подтверждение hardware потерялось.
                self._epoch += 1
                self._failure = error
                self._cv.notify_all()
                return self._epoch

        finished_at = time.monotonic()
        with self._cv:
            self._epoch = resulting_epoch
            self._metrics = StopMetrics(
                epoch=resulting_epoch,
                reason=reason,
                requested_at=requested_at,
                aborted_at=finished_at,
            )
            self._cv.notify_all()
            return self._epoch

    def wait_presented(self, epoch: int, timeout: float = 30.0) -> bool:
        """Дождаться представления всего PCM, принятого к моменту вызова.

        Это checkpoint одной клаузы: следующий текст можно считать
        произнесённым только после достижения аппаратного timeline, а не
        сразу после успешной постановки PCM в программную очередь.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            target = self._submitted_samples
            while True:
                if self._closed or epoch != self._epoch or self._failure is not None:
                    return False
                state = self._matching_state_locked()
                if state is not None and state.presented_samples_estimate >= target:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=min(remaining, 0.1))

    def wait_idle(self, epoch: int, timeout: float = 30.0) -> bool:
        """Дождаться IDLE после представления всех принятых сэмплов."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self._closed or epoch != self._epoch or self._failure is not None:
                    return False
                state = self._matching_state_locked()
                if (
                    state is not None
                    and state.state == PlaybackState.STATE_IDLE
                    and state.presented_samples_estimate >= state.submitted_samples
                ):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=min(remaining, 0.1))

    def _on_state(self, message: PlaybackState) -> None:
        with self._cv:
            self._latest_state = message
            if message.device_session_id and not self._device_session_id:
                self._device_session_id = message.device_session_id
            self._cv.notify_all()

    def _matching_state_locked(self) -> PlaybackState | None:
        state = self._latest_state
        if state is None:
            return None
        if (
            state.device_session_id != self._device_session_id
            or state.stream_id != self._stream_id
            or state.generation != self._epoch
        ):
            return None
        return state

    def _call(self, future: object, operation: str) -> object:
        return self._wait_future(future, self._service_timeout, operation)

    @staticmethod
    def _wait_future(future: object, timeout: float, operation: str) -> object:
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())  # type: ignore[attr-defined]
        if not done.wait(timeout):
            raise TimeoutError(f"{operation}: timeout {timeout:.1f} s")
        error = future.exception()  # type: ignore[attr-defined]
        if error is not None:
            raise error
        result = future.result()  # type: ignore[attr-defined]
        if result is None:
            raise SinkFailureError(f"{operation}: пустой ответ")
        return result
