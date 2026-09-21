"""Чистая политика голосовой сессии без ROS-зависимостей.

Нода-адаптер передаёт сюда sample-indexed наблюдения VAD и свежий снимок
выхода. Политика создаёт ровно один utterance_id на активный сегмент,
выдаёт admission и единственную команду cancel для подтверждённого barge-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class InputDecision(IntEnum):
    """Допуск ASR-сегмента к разным потребителям."""

    ADMIT = 0
    KWS_ONLY = 1
    REJECT = 2


class SessionState(IntEnum):
    """Наблюдаемое состояние разговорной FSM."""

    INITIALIZING = 0
    LISTENING = 1
    ROBOT_SPEAKING = 2
    BARGE_PENDING = 3
    STOPPING_OUTPUT = 4
    USER_SPEAKING = 5
    RECOGNIZING = 6
    DEGRADED = 7


@dataclass(frozen=True)
class VadWindow:
    """Одно адресованное окно VAD."""

    device_session_id: str
    first_sample: int
    sample_count: int
    timestamp: float
    probability: float
    active: bool
    discontinuity: bool = False


@dataclass(frozen=True)
class OutputSnapshot:
    """Свежесть и прерываемость текущего TTS-output."""

    speaking: bool = False
    interruptible: bool = False
    speaking_fresh: bool = False
    playback_fresh: bool = False


@dataclass(frozen=True)
class SessionAction:
    """Одно идемпотентно адресуемое решение FSM."""

    device_session_id: str
    utterance_id: int
    onset_sample: int
    onset_timestamp: float
    decision: InputDecision
    control_sequence: int
    reason: str
    cancel_output: bool = False


class VoiceSessionPolicy:
    """Последовательная FSM admission/barge-in для одного VAD-потока."""

    def __init__(
        self,
        *,
        speech_threshold: float = 0.65,
        confirm_windows: int = 3,
        automatic_barge_in_enabled: bool = False,
        audio_profile_validated: bool = False,
    ) -> None:
        """Создать политику с закрытым по умолчанию automatic barge-in."""
        if confirm_windows <= 0:
            raise ValueError("confirm_windows должен быть положительным")
        self.speech_threshold = speech_threshold
        self.confirm_windows = confirm_windows
        self.automatic_barge_in_enabled = automatic_barge_in_enabled
        self.audio_profile_validated = audio_profile_validated
        self.state = SessionState.INITIALIZING
        self.state_reason = "created"
        self.transition_sequence = 0
        self.device_session_id = ""
        self.current_utterance_id = 0
        self._next_utterance_id = 0
        self._control_sequence = 0
        self._last_window_end: int | None = None
        self._vad_active = False
        self._high_streak = 0
        self._high_onset_sample: int | None = None
        self._high_onset_timestamp = 0.0
        self._current_onset_sample = 0
        self._current_onset_timestamp = 0.0
        self._current_decision: InputDecision | None = None
        self._cancel_sent = False
        self._awaiting_final_id = 0

    def activate(self) -> None:
        """Перевести готовую FSM в режим прослушивания."""
        self._set_state(SessionState.LISTENING, "activated")

    def observe(self, window: VadWindow, output: OutputSnapshot) -> list[SessionAction]:
        """Применить соседнее окно VAD и вернуть admission/cancel действия."""
        actions: list[SessionAction] = []
        implicit_gap = (
            self._last_window_end is not None and window.first_sample != self._last_window_end
        )
        session_changed = bool(self.device_session_id) and (
            window.device_session_id != self.device_session_id
        )
        if window.discontinuity or implicit_gap or session_changed:
            actions.extend(self._reject_open_candidate("capture_discontinuity"))
            self._reset_detection()

        self.device_session_id = window.device_session_id
        self._last_window_end = window.first_sample + window.sample_count

        if window.probability >= self.speech_threshold:
            if self._high_streak == 0:
                self._high_onset_sample = window.first_sample
                self._high_onset_timestamp = window.timestamp
            self._high_streak += 1
        elif not window.active:
            self._high_streak = 0
            self._high_onset_sample = None

        rising_edge = window.active and not self._vad_active
        falling_edge = not window.active and self._vad_active
        self._vad_active = window.active

        if rising_edge:
            self._next_utterance_id += 1
            self.current_utterance_id = self._next_utterance_id
            self._current_onset_sample = (
                self._high_onset_sample
                if self._high_onset_sample is not None
                else window.first_sample
            )
            self._current_onset_timestamp = self._high_onset_timestamp or window.timestamp
            self._cancel_sent = False
            decision = InputDecision.KWS_ONLY if output.speaking else InputDecision.ADMIT
            reason = "tts_active" if output.speaking else "listening"
            actions.append(self._control(decision, reason))
            self._current_decision = decision
            self._set_state(
                SessionState.BARGE_PENDING if output.speaking else SessionState.USER_SPEAKING,
                reason,
            )

        if (
            self._current_decision == InputDecision.KWS_ONLY
            and window.active
            and self._high_streak >= self.confirm_windows
            and self._barge_guards_pass(output)
        ):
            actions.append(self._control(InputDecision.ADMIT, "barge_in_confirmed", cancel=True))
            self._current_decision = InputDecision.ADMIT
            self._cancel_sent = True
            self._set_state(SessionState.STOPPING_OUTPUT, "barge_in_confirmed")

        if falling_edge:
            if self._current_decision == InputDecision.KWS_ONLY:
                actions.append(self._control(InputDecision.REJECT, "kws_candidate_ended"))
                self.current_utterance_id = 0
                self._set_state(
                    SessionState.ROBOT_SPEAKING if output.speaking else SessionState.LISTENING,
                    "candidate_rejected",
                )
            elif self._current_decision == InputDecision.ADMIT:
                self._awaiting_final_id = self.current_utterance_id
                self._set_state(SessionState.RECOGNIZING, "vad_endpoint")
            self._current_decision = None
            self._high_streak = 0
            self._high_onset_sample = None

        self.update_output(output)
        return actions

    def update_output(self, output: OutputSnapshot) -> None:
        """Отразить фактический старт/stop TTS между окнами VAD."""
        if self.state == SessionState.STOPPING_OUTPUT:
            if not output.speaking and output.speaking_fresh:
                next_state = (
                    SessionState.USER_SPEAKING if self._vad_active else SessionState.RECOGNIZING
                )
                self._set_state(next_state, "output_stopped")
            return
        if self._current_decision == InputDecision.KWS_ONLY:
            self._set_state(SessionState.BARGE_PENDING, "kws_only_candidate")
            return
        if self._vad_active and self._current_decision == InputDecision.ADMIT:
            self._set_state(SessionState.USER_SPEAKING, "utterance_admitted")
            return
        if self._awaiting_final_id:
            self._set_state(SessionState.RECOGNIZING, "awaiting_final")
            return
        self._set_state(
            SessionState.ROBOT_SPEAKING if output.speaking else SessionState.LISTENING,
            "playback_active" if output.speaking else "idle",
        )

    def finish_utterance(self, utterance_id: int, status: str) -> None:
        """Закрыть ожидание final, не реагируя на позднее чужое событие."""
        if utterance_id != self._awaiting_final_id:
            return
        self._awaiting_final_id = 0
        self.current_utterance_id = 0
        self._set_state(SessionState.LISTENING, status)

    def _barge_guards_pass(self, output: OutputSnapshot) -> bool:
        return (
            self.automatic_barge_in_enabled
            and self.audio_profile_validated
            and output.speaking
            and output.interruptible
            and output.speaking_fresh
            and output.playback_fresh
            and not self._cancel_sent
        )

    def _control(
        self, decision: InputDecision, reason: str, *, cancel: bool = False
    ) -> SessionAction:
        self._control_sequence += 1
        return SessionAction(
            device_session_id=self.device_session_id,
            utterance_id=self.current_utterance_id,
            onset_sample=self._current_onset_sample,
            onset_timestamp=self._current_onset_timestamp,
            decision=decision,
            control_sequence=self._control_sequence,
            reason=reason,
            cancel_output=cancel,
        )

    def _reject_open_candidate(self, reason: str) -> list[SessionAction]:
        if self._current_decision is None or not self.current_utterance_id:
            return []
        return [self._control(InputDecision.REJECT, reason)]

    def _reset_detection(self) -> None:
        self._last_window_end = None
        self._vad_active = False
        self._high_streak = 0
        self._high_onset_sample = None
        self._current_decision = None
        self._cancel_sent = False
        self._awaiting_final_id = 0
        self.current_utterance_id = 0
        self._set_state(SessionState.LISTENING, "capture_reset")

    def _set_state(self, state: SessionState, reason: str) -> None:
        if state != self.state or reason != self.state_reason:
            self.transition_sequence += 1
            self.state = state
            self.state_reason = reason
