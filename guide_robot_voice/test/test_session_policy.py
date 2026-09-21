"""FSM голосовой сессии: admission, ID и guards автоматического barge-in."""

from __future__ import annotations

from guide_robot_voice.lib.session_policy import (
    InputDecision,
    OutputSnapshot,
    SessionState,
    VadWindow,
    VoiceSessionPolicy,
)

WINDOW = 512


def observation(
    index: int,
    *,
    probability: float,
    active: bool,
    session: str = "capture-a",
    discontinuity: bool = False,
) -> VadWindow:
    return VadWindow(
        device_session_id=session,
        first_sample=index * WINDOW,
        sample_count=WINDOW,
        timestamp=index * WINDOW / 16_000,
        probability=probability,
        active=active,
        discontinuity=discontinuity,
    )


def test_listening_admits_one_utterance_from_first_speech_window() -> None:
    policy = VoiceSessionPolicy()
    policy.activate()
    quiet = OutputSnapshot(speaking_fresh=True)

    assert policy.observe(observation(0, probability=0.8, active=False), quiet) == []
    actions = policy.observe(observation(1, probability=0.9, active=True), quiet)

    assert len(actions) == 1
    assert actions[0].decision == InputDecision.ADMIT
    assert actions[0].utterance_id == 1
    assert actions[0].onset_sample == 0
    assert actions[0].cancel_output is False
    assert policy.state == SessionState.USER_SPEAKING


def test_tts_candidate_is_kws_only_while_automatic_barge_in_is_closed() -> None:
    policy = VoiceSessionPolicy(
        confirm_windows=3,
        automatic_barge_in_enabled=False,
        audio_profile_validated=True,
    )
    policy.activate()
    output = OutputSnapshot(True, True, True, True)

    policy.observe(observation(0, probability=0.8, active=False), output)
    opened = policy.observe(observation(1, probability=0.9, active=True), output)
    later = policy.observe(observation(2, probability=0.9, active=True), output)

    assert [item.decision for item in opened] == [InputDecision.KWS_ONLY]
    assert later == []
    assert policy.state == SessionState.BARGE_PENDING


def test_validated_interruptible_output_is_cancelled_once_after_confirmation() -> None:
    policy = VoiceSessionPolicy(
        confirm_windows=3,
        automatic_barge_in_enabled=True,
        audio_profile_validated=True,
    )
    policy.activate()
    output = OutputSnapshot(True, True, True, True)

    policy.observe(observation(0, probability=0.8, active=False), output)
    opened = policy.observe(observation(1, probability=0.9, active=True), output)
    confirmed = policy.observe(observation(2, probability=0.9, active=True), output)
    duplicate = policy.observe(observation(3, probability=0.9, active=True), output)

    assert opened[0].decision == InputDecision.KWS_ONLY
    assert confirmed[0].decision == InputDecision.ADMIT
    assert confirmed[0].utterance_id == opened[0].utterance_id
    assert confirmed[0].cancel_output is True
    assert duplicate == []
    assert policy.state == SessionState.STOPPING_OUTPUT


def test_non_interruptible_output_never_passes_barge_guards() -> None:
    policy = VoiceSessionPolicy(
        confirm_windows=2,
        automatic_barge_in_enabled=True,
        audio_profile_validated=True,
    )
    policy.activate()
    protected = OutputSnapshot(True, False, True, True)

    policy.observe(observation(0, probability=0.9, active=False), protected)
    actions = policy.observe(observation(1, probability=0.9, active=True), protected)

    assert [item.decision for item in actions] == [InputDecision.KWS_ONLY]
    assert all(not item.cancel_output for item in actions)


def test_capture_discontinuity_rejects_open_candidate_and_starts_new_identity() -> None:
    policy = VoiceSessionPolicy()
    policy.activate()
    output = OutputSnapshot(True, True, True, True)
    policy.observe(observation(0, probability=0.9, active=False), output)
    opened = policy.observe(observation(1, probability=0.9, active=True), output)

    reset_actions = policy.observe(
        observation(
            0,
            probability=0.0,
            active=False,
            session="capture-b",
            discontinuity=True,
        ),
        output,
    )

    assert opened[0].utterance_id == 1
    assert reset_actions[0].decision == InputDecision.REJECT
    assert reset_actions[0].device_session_id == "capture-a"
    assert policy.device_session_id == "capture-b"
    assert policy.current_utterance_id == 0


def test_late_final_cannot_close_a_newer_utterance() -> None:
    policy = VoiceSessionPolicy()
    policy.activate()
    output = OutputSnapshot(speaking_fresh=True)
    policy.observe(observation(0, probability=0.9, active=False), output)
    policy.observe(observation(1, probability=0.9, active=True), output)
    policy.observe(observation(2, probability=0.0, active=False), output)
    assert policy.state == SessionState.RECOGNIZING

    policy.finish_utterance(999, "late_final")
    assert policy.state == SessionState.RECOGNIZING

    policy.finish_utterance(1, "final")
    assert policy.state == SessionState.LISTENING
