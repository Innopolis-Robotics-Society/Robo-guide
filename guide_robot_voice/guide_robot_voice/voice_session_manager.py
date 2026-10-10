"""ROS-адаптер голосовой FSM и единственный automatic barge-in owner.

VAD сообщает измерение, ASR владеет PCM-сегментом, TTS владеет очередью Say,
а эта нода принимает admission-решение для конкретного utterance_id. В
XVF-профиле автоматическая отмена остаётся закрыта, пока аппаратный AEC-профиль
явно не помечен проверенным.
"""

from __future__ import annotations

import math
import threading

import rclpy
from guide_robot_msgs.msg import (
    CancelAll,
    DialogTurnEvent,
    PlaybackState,
    SpeakingStatus,
    UtteranceControl,
    UtteranceEvent,
    VadObservation,
    VoiceSessionState,
    Wakeword,
    WakewordCandidate,
)
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_voice.lib.qos import (
    QOS_CANCEL_ALL,
    QOS_DIALOG_TURN_EVENT,
    QOS_PLAYBACK_STATE,
    QOS_UTTERANCE_CONTROL,
    QOS_UTTERANCE_EVENT,
    QOS_VAD_OBSERVATION,
    QOS_VOICE_SESSION_STATE,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_voice.lib.session_policy import (
    OutputSnapshot,
    SessionAction,
    VadWindow,
    VoiceSessionPolicy,
)
from guide_robot_voice.lib.wakeword_session_policy import WakewordSessionPolicy

_STATUS_STALE_SEC = 0.4
_MAX_FUTURE_SKEW_SEC = 0.1
_WAKE_MAX_AGE_SEC = 0.5
_ACTIVE_PLAYBACK_STATES = {
    PlaybackState.STATE_BUFFERING,
    PlaybackState.STATE_PLAYING,
    PlaybackState.STATE_DRAINING,
}
_STOPPED_PLAYBACK_STATES = {
    PlaybackState.STATE_IDLE,
    PlaybackState.STATE_FENCED,
}


class VoiceSessionManager(LifecycleNode):
    """Координатор VAD admission, ASR-сегмента и остановки output."""

    def __init__(self) -> None:
        """Объявить параметры; ROS-интерфейсы создаются при configure."""
        super().__init__("voice_session_manager")
        self.declare_parameter("speech_threshold", 0.65)
        self.declare_parameter("barge_confirm_windows", 3)
        self.declare_parameter("automatic_barge_in_enabled", False)
        self.declare_parameter("audio_profile_validated", False)
        self.declare_parameter("heartbeat_hz", 5.0)
        self.declare_parameter("wakeword_gated", False)
        self.declare_parameter("command_window_seconds", 5.0)
        self.declare_parameter("followup_window_seconds", 0.0)
        self.declare_parameter("echo_hold_seconds", 0.4)
        self.declare_parameter("wakeword_barge_in_enabled", False)
        self.declare_parameter("allow_unvalidated_wakeword_barge_in", False)
        self.declare_parameter("wakeword_model_sha256", "")
        self.declare_parameter("wakeword_min_confidence", 0.8601541979408264)

        self._policy: VoiceSessionPolicy | None = None
        self._latest_speaking: SpeakingStatus | None = None
        self._latest_playback: PlaybackState | None = None
        self._active = False
        self._lock = threading.Lock()
        self._last_cancel_epoch = 0
        self._accepted_wakes = 0
        self._rejected_wakes = 0
        self._last_window_deadline = 0.0

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Создать чистую FSM и её ROS-интерфейсы."""
        del state
        try:
            gated = bool(self.get_parameter("wakeword_gated").value)
            if gated and not str(self.get_parameter("wakeword_model_sha256").value):
                raise ValueError("wakeword_gated требует wakeword_model_sha256")
            policy_type = WakewordSessionPolicy if gated else VoiceSessionPolicy
            options = (
                {
                    name: self.get_parameter(name).value
                    for name in (
                        "command_window_seconds",
                        "followup_window_seconds",
                        "echo_hold_seconds",
                        "wakeword_barge_in_enabled",
                        "allow_unvalidated_wakeword_barge_in",
                    )
                }
                if gated
                else {}
            )
            self._policy = policy_type(
                speech_threshold=float(self.get_parameter("speech_threshold").value),
                confirm_windows=int(self.get_parameter("barge_confirm_windows").value),
                automatic_barge_in_enabled=bool(
                    self.get_parameter("automatic_barge_in_enabled").value
                ),
                audio_profile_validated=bool(self.get_parameter("audio_profile_validated").value),
                **options,
            )
            self._control_pub = self.create_lifecycle_publisher(
                UtteranceControl, "/voice/input_control", QOS_UTTERANCE_CONTROL
            )
            self._state_pub = self.create_lifecycle_publisher(
                VoiceSessionState, "/voice/session_state", QOS_VOICE_SESSION_STATE
            )
            self._cancel_pub = self.create_lifecycle_publisher(
                CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL
            )
            if gated:
                self._wakeword_pub = self.create_lifecycle_publisher(
                    Wakeword, "/speech/wakeword", QOS_WAKEWORD
                )
                self._candidate_sub = self.create_subscription(
                    WakewordCandidate,
                    "/voice/wakeword_candidate",
                    self._on_wakeword_candidate,
                    QOS_WAKEWORD,
                )
                self._dialog_turn_sub = self.create_subscription(
                    DialogTurnEvent,
                    "/dialog/turn_event",
                    self._on_dialog_turn,
                    QOS_DIALOG_TURN_EVENT,
                )
            self._vad_sub = self.create_subscription(
                VadObservation,
                "/voice/vad_observation",
                self._on_vad_observation,
                QOS_VAD_OBSERVATION,
            )
            self._speaking_sub = self.create_subscription(
                SpeakingStatus,
                "/voice/speaking",
                self._on_speaking,
                QOS_VOICE_SPEAKING,
            )
            self._playback_sub = self.create_subscription(
                PlaybackState,
                "/audio/playback_state",
                self._on_playback,
                QOS_PLAYBACK_STATE,
            )
            self._event_sub = self.create_subscription(
                UtteranceEvent,
                "/voice/utterance_event",
                self._on_utterance_event,
                QOS_UTTERANCE_EVENT,
            )
            heartbeat_hz = float(self.get_parameter("heartbeat_hz").value)
            self._heartbeat = self.create_timer(1.0 / max(heartbeat_hz, 0.1), self._publish_state)
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

        automatic = bool(self.get_parameter("automatic_barge_in_enabled").value)
        validated = bool(self.get_parameter("audio_profile_validated").value)
        self.get_logger().info(
            f"voice_session_manager сконфигурирован: automatic_barge_in={automatic}, "
            f"audio_profile_validated={validated}, wakeword_gated={gated}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Начать принимать VAD-наблюдения и публиковать heartbeat."""
        result = super().on_activate(state)
        if result != TransitionCallbackReturn.SUCCESS:
            return result
        assert self._policy is not None
        with self._lock:
            self._policy.activate()
            self._latest_speaking = None
            self._latest_playback = None
            self._active = True
            self._last_window_deadline = 0.0
        self._publish_state()
        return TransitionCallbackReturn.SUCCESS

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Остановить принятие решений до следующей активации."""
        with self._lock:
            self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Удалить интерфейсы, чтобы reconfigure не дублировал admission."""
        del state
        with self._lock:
            self._active = False
            self._policy = None
        for name in (
            "_vad_sub",
            "_speaking_sub",
            "_playback_sub",
            "_event_sub",
            "_candidate_sub",
            "_dialog_turn_sub",
        ):
            entity = getattr(self, name, None)
            if entity is not None:
                self.destroy_subscription(entity)
                setattr(self, name, None)
        timer = getattr(self, "_heartbeat", None)
        if timer is not None:
            self.destroy_timer(timer)
            self._heartbeat = None
        for name in ("_control_pub", "_state_pub", "_cancel_pub", "_wakeword_pub"):
            entity = getattr(self, name, None)
            if entity is not None:
                self.destroy_lifecycle_publisher(entity)
                setattr(self, name, None)
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же безопасное освобождение, что при cleanup."""
        return self.on_cleanup(state)

    def _on_vad_observation(self, msg: VadObservation) -> None:
        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        window = VadWindow(
            device_session_id=msg.device_session_id,
            first_sample=int(msg.first_sample),
            sample_count=int(msg.sample_count),
            timestamp=timestamp,
            probability=float(msg.probability),
            active=bool(msg.active),
            discontinuity=bool(msg.discontinuity),
        )
        with self._lock:
            if not self._active or self._policy is None:
                return
            actions = self._policy.observe(window, self._output_snapshot(msg.device_session_id))
        for action in actions:
            self._publish_action(action)
        self._publish_state()

    def _on_wakeword_candidate(self, msg: WakewordCandidate) -> None:
        """Только принятое событие становится публичным wake и admission."""
        timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - timestamp
        with self._lock:
            if not self._active or not isinstance(self._policy, WakewordSessionPolicy):
                return
            if (
                not -_MAX_FUTURE_SKEW_SEC <= age <= _WAKE_MAX_AGE_SEC
                or msg.keyword != "фирая"
                or msg.model_sha256 != str(self.get_parameter("wakeword_model_sha256").value)
                or not math.isfinite(msg.confidence)
                or not float(self.get_parameter("wakeword_min_confidence").value)
                <= msg.confidence
                <= 1
            ):
                self._rejected_wakes += 1
                return
            output = self._output_snapshot(msg.device_session_id)
            actions = self._policy.accept_wake(
                msg.device_session_id, int(msg.end_sample), timestamp, output
            )
            if not actions:
                self._rejected_wakes += 1
                return
            self._accepted_wakes += 1
        # Wake ДО control: ASR может быстро закончить короткую реплику.
        accepted = Wakeword()
        accepted.header.stamp = self.get_clock().now().to_msg()
        accepted.header.frame_id = msg.header.frame_id
        accepted.keyword = msg.keyword
        accepted.confidence = msg.confidence
        accepted.tts_active = output.speaking
        accepted.azimuth = float("nan")
        self._wakeword_pub.publish(accepted)
        for action in actions:
            self._publish_action(action)
        self.get_logger().info(
            f"KWS принят: score={msg.confidence:.3f}, end_sample={msg.end_sample}, "
            f"utterance_id={actions[-1].utterance_id}"
        )
        self._publish_state()

    def _on_speaking(self, msg: SpeakingStatus) -> None:
        with self._lock:
            self._latest_speaking = msg
            if self._active and self._policy is not None:
                self._policy.update_output(self._output_snapshot(self._policy.device_session_id))

    def _on_playback(self, msg: PlaybackState) -> None:
        with self._lock:
            self._latest_playback = msg
            if self._active and self._policy is not None:
                self._policy.update_output(self._output_snapshot(self._policy.device_session_id))
        self._publish_state()

    def _on_utterance_event(self, msg: UtteranceEvent) -> None:
        if msg.event not in (
            UtteranceEvent.EVENT_FINAL,
            UtteranceEvent.EVENT_DISCARDED,
            UtteranceEvent.EVENT_ENDPOINT,
        ):
            return
        with self._lock:
            if not self._active or self._policy is None:
                return
            if msg.device_session_id != self._policy.device_session_id:
                return
            if msg.event == UtteranceEvent.EVENT_ENDPOINT:
                if isinstance(self._policy, WakewordSessionPolicy):
                    self._policy.endpoint(int(msg.utterance_id))
            else:
                if isinstance(self._policy, WakewordSessionPolicy):
                    self._policy.finish_utterance(
                        int(msg.utterance_id), msg.status, int(msg.start_sample)
                    )
                else:
                    self._policy.finish_utterance(int(msg.utterance_id), msg.status)
                if (
                    isinstance(self._policy, WakewordSessionPolicy)
                    and self._policy.state_reason == "wakeword_only"
                ):
                    self.get_logger().info(
                        f"Имя принято, окно команды: {self._policy.command_window_seconds:.1f} с "
                        "(без повторного имени)"
                    )
            self._policy.update_output(self._output_snapshot(self._policy.device_session_id))
        self._publish_state()

    def _on_dialog_turn(self, msg: DialogTurnEvent) -> None:
        if not self._stamp_is_fresh(msg.stamp):
            return
        with self._lock:
            if not self._active or not isinstance(self._policy, WakewordSessionPolicy):
                return
            self._policy.reply_finished(
                msg.device_session_id,
                int(msg.utterance_id),
                int(msg.start_sample),
                msg.stamp.sec + msg.stamp.nanosec / 1e9,
                bool(msg.reply_completed),
            )
        self._publish_state()

    def _output_snapshot(self, device_session_id: str) -> OutputSnapshot:
        speaking = self._latest_speaking
        playback = self._latest_playback
        speaking_status_fresh = speaking is not None and self._stamp_is_fresh(speaking.stamp)
        playback_status_fresh = (
            playback is not None
            and playback.device_session_id == device_session_id
            and self._stamp_is_fresh(playback.stamp)
        )
        playback_active = bool(
            playback_status_fresh
            and playback is not None
            and playback.state in _ACTIVE_PLAYBACK_STATES
        )
        output_active = bool(
            (speaking is not None and speaking.speaking and speaking_status_fresh)
            or playback_active
        )
        return OutputSnapshot(
            speaking=output_active,
            interruptible=bool(
                speaking is not None and speaking.interruptible and speaking_status_fresh
            ),
            # Для акустического wake не угадываем отсутствие output по одному
            # heartbeat: TTS и hardware-owner должны быть оба свежими.
            speaking_fresh=(speaking_status_fresh and playback_status_fresh)
            if bool(self.get_parameter("wakeword_gated").value)
            else speaking_status_fresh or playback_status_fresh,
            playback_fresh=playback_active,
            playback_stopped=bool(
                playback_status_fresh
                and playback is not None
                and playback.state in _STOPPED_PLAYBACK_STATES
                and playback.buffered_samples == 0
            ),
        )

    def _stamp_is_fresh(self, stamp: object) -> bool:
        stamp_sec = stamp.sec + stamp.nanosec / 1e9  # type: ignore[attr-defined]
        age = self.get_clock().now().nanoseconds / 1e9 - stamp_sec
        return -_MAX_FUTURE_SKEW_SEC <= age <= _STATUS_STALE_SEC

    def _publish_action(self, action: SessionAction) -> None:
        control = UtteranceControl()
        control.stamp = self.get_clock().now().to_msg()
        control.device_session_id = action.device_session_id
        control.utterance_id = action.utterance_id
        control.onset_sample = action.onset_sample
        control.decision = int(action.decision)
        control.control_sequence = action.control_sequence
        control.reason = action.reason
        self._control_pub.publish(control)
        if action.reason in ("command_after_name", "followup_without_wakeword"):
            self.get_logger().info(
                f"Команда без повторного имени принята ({action.reason}): "
                f"utterance_id={action.utterance_id}, "
                f"onset_sample={action.onset_sample}"
            )

        if not action.cancel_output:
            return
        cancel = CancelAll()
        cancel.stamp = self._seconds_to_time_msg(action.onset_timestamp)
        now_epoch = self.get_clock().now().nanoseconds
        self._last_cancel_epoch = max(self._last_cancel_epoch + 1, now_epoch)
        cancel.epoch = self._last_cancel_epoch
        cancel.scope = CancelAll.SCOPE_ALL
        cancel.reason = CancelAll.REASON_BARGE_IN
        self._cancel_pub.publish(cancel)
        self.get_logger().info(
            f"barge-in принят: utterance_id={action.utterance_id}, "
            f"onset_sample={action.onset_sample}"
        )

    def _publish_state(self) -> None:
        with self._lock:
            if not self._active or self._policy is None:
                return
            state = VoiceSessionState()
            if isinstance(self._policy, WakewordSessionPolicy):
                self._policy.tick(
                    self.get_clock().now().nanoseconds / 1e9,
                    self._output_snapshot(self._policy.device_session_id),
                )
                if (
                    self._policy.command_window_open
                    and self._policy.command_until != self._last_window_deadline
                ):
                    self._last_window_deadline = self._policy.command_until
                    self.get_logger().info(
                        f"Окно без повторного имени открыто ({self._policy.command_window_kind}): "
                        f"до {self._policy.command_until:.3f}"
                    )
            state.stamp = self.get_clock().now().to_msg()
            state.state = int(self._policy.state)
            state.device_session_id = self._policy.device_session_id
            state.utterance_id = self._policy.current_utterance_id
            state.transition_sequence = self._policy.transition_sequence
            state.reason = self._policy.state_reason
        self._state_pub.publish(state)

    @staticmethod
    def _seconds_to_time_msg(seconds: float) -> object:
        from builtin_interfaces.msg import Time as TimeMsg

        sec = int(seconds)
        nanosec = round((seconds - sec) * 1e9)
        return TimeMsg(sec=sec, nanosec=nanosec)


def main(args: list[str] | None = None) -> None:
    """Запустить voice_session_manager."""
    rclpy.init(args=args)
    node = VoiceSessionManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
