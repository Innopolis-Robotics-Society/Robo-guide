"""Fail-closed acoustic-wake admission; legacy VAD policy остаётся отдельно."""

from __future__ import annotations

from guide_robot_voice.lib.session_policy import (
    InputDecision,
    OutputSnapshot,
    SessionAction,
    SessionState,
    VadWindow,
    VoiceSessionPolicy,
)

_MAX_WINDOW_SECONDS = 30.0
_MAX_ECHO_HOLD_SECONDS = 2.0
_SAMPLE_RATE = 16_000
_REWAKE_MIN_SECONDS = 1.5


class WakewordSessionPolicy(VoiceSessionPolicy):
    """В ARMED VAD никогда не открывает ASR, даже KWS_ONLY."""

    def __init__(
        self,
        *,
        command_window_seconds: float = 5.0,
        followup_window_seconds: float = 0.0,
        echo_hold_seconds: float = 0.4,
        wakeword_barge_in_enabled: bool = False,
        allow_unvalidated_wakeword_barge_in: bool = False,
        **kwargs: float | int | bool,
    ) -> None:
        """Создать закрытый gate и ограниченное окно после голого имени."""
        super().__init__(**kwargs)
        if not 0 < command_window_seconds <= _MAX_WINDOW_SECONDS:
            raise ValueError("command_window_seconds: (0, 30]")
        if (
            not 0 <= followup_window_seconds <= _MAX_WINDOW_SECONDS
            or not 0 <= echo_hold_seconds <= _MAX_ECHO_HOLD_SECONDS
        ):
            raise ValueError("followup: [0, 30], echo hold: [0, 2]")
        self.command_window_seconds = command_window_seconds
        self.followup_window_seconds = followup_window_seconds
        self.echo_hold_seconds = echo_hold_seconds
        self.wakeword_barge_in_enabled = wakeword_barge_in_enabled
        self.allow_unvalidated_wakeword_barge_in = allow_unvalidated_wakeword_barge_in
        self.command_until = 0.0
        self._awaiting_command = False
        self._last_wake_end = -1
        self._capture_floor = 0
        self._latest_window: VadWindow | None = None
        self._speech_onset_sample = 0
        self._speech_onset_timestamp = 0.0
        self._window_kind = "command_after_name"
        self._window_floor = 0
        self._window_time_floor = 0.0
        self._last_wake_timestamp = 0.0
        self._current_reason = ""
        self._expected_reply: tuple[int, int] | None = None
        self._pending_window = ""
        self._pending_until = 0.0
        self._pending_started = 0.0
        self._quiet_since: float | None = None

    @property
    def command_window_open(self) -> bool:
        """Открыто ли одноразовое окно новой реплики без повторного имени."""
        return self._awaiting_command

    @property
    def command_window_kind(self) -> str:
        """Причина открытия текущего окна для индикации и логирования."""
        return self._window_kind

    def activate(self) -> None:
        """Готовность слушать имя, не общую речь."""
        self.command_until = 0.0
        self._awaiting_command = False
        self._reset_detection()
        self.device_session_id = ""
        self._latest_window = None
        self._speech_onset_sample = 0
        self._speech_onset_timestamp = 0.0
        self._last_wake_end = -1
        self._last_wake_timestamp = 0.0
        self._capture_floor = 0
        self._current_reason = ""
        self._expected_reply = None
        self._pending_window = ""
        self._pending_until = 0.0
        self._pending_started = 0.0
        self._quiet_since = None
        self._window_floor = 0
        self._window_time_floor = 0.0
        self._window_kind = "command_after_name"
        self._set_state(SessionState.ARMED, "awaiting_wakeword")

    def observe(self, window: VadWindow, output: OutputSnapshot) -> list[SessionAction]:
        """VAD завершает допущенную реплику, но не разрешает фоновый decode."""
        actions = []
        gap = self._last_window_end is not None and window.first_sample != self._last_window_end
        changed = bool(self.device_session_id) and (
            self.device_session_id != window.device_session_id
        )
        if window.discontinuity or gap or changed:
            actions.extend(self._reject_open_candidate("capture_discontinuity"))
            self._reset_detection()
            self.command_until = 0.0
            self._awaiting_command = False
            self._expected_reply = None
            self._pending_window = ""
            self._quiet_since = None
            self._capture_floor = window.first_sample
            if changed:
                self._last_wake_end = -1
        elif self._last_window_end is None:
            self._capture_floor = window.first_sample
        self.device_session_id = window.device_session_id
        self._last_window_end = window.first_sample + window.sample_count
        self._latest_window = window
        rising = window.active and not self._vad_active
        falling = not window.active and self._vad_active
        self._vad_active = window.active
        if rising:
            self._speech_onset_sample = window.first_sample
            self._speech_onset_timestamp = window.timestamp

        if self._awaiting_command and (window.timestamp >= self.command_until or output.speaking):
            self._awaiting_command = False
        if (
            self._awaiting_command
            and window.active
            and not self.current_utterance_id
            and self._speech_onset_sample >= self._window_floor
            and self._speech_onset_timestamp >= self._window_time_floor
            and output.speaking_fresh
            and not output.speaking
        ):
            actions.append(
                self._open(
                    self._speech_onset_sample, self._speech_onset_timestamp, self._window_kind
                )
            )
            self._awaiting_command = False
        if falling and self._current_decision == InputDecision.ADMIT:
            self.endpoint(self.current_utterance_id)
        self.update_output(output)
        return actions

    def tick(self, timestamp: float, output: OutputSnapshot | None = None) -> None:
        """Окно команды истекает даже при остановившемся capture/VAD."""
        if self._awaiting_command and timestamp >= self.command_until:
            self._awaiting_command = False
            self._set_state(SessionState.ARMED, "command_timeout")
        if output is not None:
            self._poll_pending_window(timestamp, output)

    def _poll_pending_window(self, timestamp: float, output: OutputSnapshot) -> None:
        if not self._pending_window:
            return
        if timestamp < self._pending_started:
            return
        if timestamp >= self._pending_until:
            self._pending_window = ""
            self._quiet_since = None
            self.current_utterance_id = 0
            self._current_decision = None
            self._set_state(SessionState.ARMED, "output_stop_timeout")
            return
        if not output.speaking_fresh or output.speaking or not output.playback_stopped:
            self._quiet_since = None
            return
        if self._quiet_since is None:
            self._quiet_since = timestamp
        if timestamp - self._quiet_since < self.echo_hold_seconds:
            return
        self._window_kind = self._pending_window
        self._pending_window = ""
        self.current_utterance_id = 0
        self._current_decision = None
        self._awaiting_command = True
        self._window_floor = self._last_window_end or 0
        self._window_time_floor = timestamp
        duration = (
            self.followup_window_seconds
            if self._window_kind == "followup_without_wakeword"
            else self.command_window_seconds
        )
        self.command_until = timestamp + duration
        self._set_state(SessionState.LISTENING, self._window_kind)

    def reply_finished(
        self,
        session: str,
        utterance_id: int,
        start_sample: int,
        timestamp: float,
        completed: bool,
    ) -> bool:
        """Только ответ на последний final может открыть одно follow-up окно."""
        if session != self.device_session_id or self._expected_reply != (
            utterance_id,
            start_sample,
        ):
            return False
        self._expected_reply = None
        if (
            not completed
            or not self.followup_window_seconds
            or self.current_utterance_id
            or self._awaiting_command
            or self._pending_window
        ):
            return False
        self._pending_window = "followup_without_wakeword"
        self._pending_until = timestamp + 2 + self.echo_hold_seconds
        self._pending_started = timestamp
        self._quiet_since = None
        return True

    def accept_wake(
        self, session: str, end_sample: int, timestamp: float, output: OutputSnapshot
    ) -> list[SessionAction]:
        """Один wake -> одна реплика. Старые/повторные события не имеют полномочий."""
        if (
            not self.device_session_id
            or session != self.device_session_id
            or end_sample <= self._capture_floor
            or end_sample <= self._last_wake_end
            or self._last_window_end is None
            or abs(end_sample - self._last_window_end) > _SAMPLE_RATE
            or self._awaiting_final_id
            or self._pending_window
            # Повтор имени в окне -- новое обращение, а не текст команды. Второй
            # кандидат от того же произнесения окно не заменяет и не продлевает.
            or (
                self.current_utterance_id
                and self._current_reason not in ("followup_without_wakeword", "command_after_name")
            )
            or (
                self._window_kind == "command_after_name"
                and (self._awaiting_command or self.current_utterance_id)
                and timestamp - self._last_wake_timestamp < _REWAKE_MIN_SECONDS
            )
            or not output.speaking_fresh
        ):
            return []
        named_barge_allowed = (
            self.wakeword_barge_in_enabled
            and (self.audio_profile_validated or self.allow_unvalidated_wakeword_barge_in)
            and output.interruptible
            and output.speaking_fresh
            and output.playback_fresh
        )
        if output.speaking and not (named_barge_allowed or self._barge_guards_pass(output)):
            return []
        actions = []
        if self.current_utterance_id:
            actions.extend(self._reject_open_candidate("superseded_by_wakeword"))
        self._expected_reply = None
        self._last_wake_end = end_sample
        self._last_wake_timestamp = timestamp
        self._awaiting_command = False
        action = self._open(end_sample, timestamp, "acoustic_wakeword")
        if output.speaking:
            # Сначала stop+ACK, не ADMIT смеси колонки и имени в ASR.
            self._current_decision = InputDecision.REJECT
            self._pending_window = "command_after_name"
            self._pending_until = timestamp + 2 + self.echo_hold_seconds
            self._pending_started = timestamp
            self._quiet_since = None
            self._set_state(SessionState.STOPPING_OUTPUT, "wakeword_stop")
            return [*actions, self._control(InputDecision.REJECT, "wakeword_stop", cancel=True)]
        self.command_until = timestamp + self.command_window_seconds
        return [*actions, action]

    def _open(
        self, sample: int, timestamp: float, reason: str, *, cancel: bool = False
    ) -> SessionAction:
        self._next_utterance_id += 1
        self.current_utterance_id = self._next_utterance_id
        self._current_onset_sample = sample
        self._current_onset_timestamp = timestamp
        self._current_decision = InputDecision.ADMIT
        self._current_reason = reason
        self._awaiting_final_id = 0
        self._cancel_sent = cancel
        self._set_state(SessionState.STOPPING_OUTPUT if cancel else SessionState.LISTENING, reason)
        return self._control(InputDecision.ADMIT, reason, cancel=cancel)

    def endpoint(self, utterance_id: int) -> None:
        """Учитывать и VAD endpoint, и ASR max-duration endpoint в непрерывной толпе."""
        if utterance_id != self.current_utterance_id or not utterance_id:
            return
        self._awaiting_final_id = utterance_id
        self._current_decision = None
        self._set_state(SessionState.RECOGNIZING, "awaiting_final")

    def _reject_open_candidate(self, reason: str) -> list[SessionAction]:
        # REJECT fencing нужен и после endpoint, пока final ещё в воркере.
        if not self.current_utterance_id:
            return []
        return [self._control(InputDecision.REJECT, reason)]

    def finish_utterance(self, utterance_id: int, status: str, start_sample: int = 0) -> None:
        """После имени оставить 5 с; после содержательного final немедленно закрыть."""
        if not utterance_id or utterance_id != self.current_utterance_id:
            return
        self._awaiting_final_id = 0
        self.current_utterance_id = 0
        self._current_decision = None
        self._expected_reply = (utterance_id, start_sample) if status == "final" else None
        self._awaiting_command = status == "wakeword_only"
        self._window_kind = "command_after_name"
        self._window_floor = self._last_wake_end
        self._window_time_floor = self._last_wake_timestamp
        if self._awaiting_command and self._latest_window is not None:
            self.command_until = self._latest_window.timestamp + self.command_window_seconds
        self._set_state(
            SessionState.LISTENING if self._awaiting_command else SessionState.ARMED, status
        )

    def update_output(self, output: OutputSnapshot) -> None:
        """Индикация следует admission, не активности толпы."""
        if self._latest_window is not None:
            self._poll_pending_window(self._latest_window.timestamp, output)
        if self._pending_window:
            if self._pending_window == "command_after_name":
                self._set_state(SessionState.STOPPING_OUTPUT, "wakeword_stop")
            return
        if self.state == SessionState.STOPPING_OUTPUT and self.current_utterance_id:
            if not output.speaking and output.speaking_fresh and output.playback_stopped:
                self._set_state(SessionState.USER_SPEAKING, "output_stopped")
            return
        if self._awaiting_final_id:
            self._set_state(SessionState.RECOGNIZING, "awaiting_final")
        elif self._current_decision == InputDecision.ADMIT:
            self._set_state(
                SessionState.USER_SPEAKING if self._vad_active else SessionState.LISTENING,
                "utterance_admitted",
            )
        elif self._awaiting_command:
            self._set_state(SessionState.LISTENING, self._window_kind)
        else:
            self._set_state(SessionState.ARMED, "awaiting_wakeword")
