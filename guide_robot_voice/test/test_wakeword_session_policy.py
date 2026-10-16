"""Wake gate: фоновый VAD без decode, одна реплика, bounded command window."""

import pytest

from guide_robot_voice.lib.session_policy import (
    InputDecision,
    OutputSnapshot,
    SessionState,
    VadWindow,
)
from guide_robot_voice.lib.wakeword_session_policy import WakewordSessionPolicy

IDLE = OutputSnapshot(speaking_fresh=True, playback_stopped=True)
SPEAKING = OutputSnapshot(True, True, True, True)


def window(n, active=True, session="a", discontinuity=False):
    return VadWindow(session, n * 512, 512, n * 0.032, 0.9 if active else 0, active, discontinuity)


def armed(**kwargs):
    policy = WakewordSessionPolicy(**kwargs)
    policy.activate()
    policy.observe(window(0), IDLE)
    return policy


def test_background_speech_never_opens_asr_including_under_tts():
    policy = armed()
    for n in range(1, 300):
        assert policy.observe(window(n, n % 10 < 8), IDLE if n % 2 else SPEAKING) == []
    assert policy.state == SessionState.ARMED and policy.current_utterance_id == 0


def test_wake_in_continuous_crowd_opens_at_trigger_not_start_of_crowd():
    policy = armed()
    for n in range(1, 60):
        policy.observe(window(n), IDLE)
    (action,) = policy.accept_wake("a", 30000, 1.875, IDLE)
    assert action.decision == InputDecision.ADMIT
    assert action.onset_sample == 30000 and action.cancel_output is False
    assert policy.accept_wake("a", 32000, 2, IDLE) == []
    policy.endpoint(action.utterance_id)
    policy.finish_utterance(action.utterance_id, "final")
    assert policy.state == SessionState.ARMED
    assert policy.observe(window(60), IDLE) == []


@pytest.mark.parametrize("session,end", [("b", 512), ("a", 0), ("a", 20000)])
def test_wrong_capture_and_stale_samples_rejected(session, end):
    assert armed().accept_wake(session, end, 0.032, IDLE) == []


def test_duplicate_wake_cannot_reopen_after_final():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.finish_utterance(action.utterance_id, "final")
    assert policy.accept_wake("a", 512, 0.032, IDLE) == []


def test_only_name_opens_one_bounded_command_without_repeated_name():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.observe(window(1, False), IDLE)
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    assert policy.state == SessionState.LISTENING
    (command,) = policy.observe(window(2), IDLE)
    assert command.reason == "command_after_name"
    assert command.utterance_id > action.utterance_id
    policy.endpoint(command.utterance_id)
    policy.finish_utterance(command.utterance_id, "final")
    policy.observe(window(3, False), IDLE)
    assert policy.observe(window(4), IDLE) == []


def test_command_window_expires_and_cannot_interrupt_tts():
    policy = armed(command_window_seconds=0.05)
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.observe(window(1, False), IDLE)
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    assert policy.observe(window(2), SPEAKING) == []
    policy.observe(window(3, False), IDLE)
    assert policy.state == SessionState.ARMED
    assert policy.observe(window(4), IDLE) == []


def test_capture_gap_rejects_even_already_queued_final():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.endpoint(action.utterance_id)
    (rejection,) = policy.observe(window(10, discontinuity=True), IDLE)
    assert rejection.decision == InputDecision.REJECT
    assert rejection.utterance_id == action.utterance_id
    assert policy.state == SessionState.ARMED
    assert policy.accept_wake("a", 512, 0.032, IDLE) == []


@pytest.mark.parametrize(
    "kwargs,output,accepted",
    [
        ({}, SPEAKING, False),
        ({"automatic_barge_in_enabled": True}, SPEAKING, False),
        ({"audio_profile_validated": True}, SPEAKING, False),
        ({"automatic_barge_in_enabled": True, "audio_profile_validated": True}, SPEAKING, True),
        (
            {"automatic_barge_in_enabled": True, "audio_profile_validated": True},
            OutputSnapshot(True, False, True, True),
            False,
        ),
        ({}, OutputSnapshot(speaking_fresh=False), False),
    ],
)
def test_tts_requires_all_guards(kwargs, output, accepted):
    policy = armed(**kwargs)
    actions = policy.accept_wake("a", 512, 0.032, output)
    assert bool(actions) == accepted
    if accepted:
        assert actions[0].cancel_output
        assert policy.state == SessionState.STOPPING_OUTPUT


def test_late_final_does_not_close_new_admission():
    policy = armed()
    (first,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.finish_utterance(first.utterance_id, "final")
    (second,) = policy.accept_wake("a", 1024, 0.064, IDLE)
    policy.finish_utterance(first.utterance_id, "late")
    assert policy.current_utterance_id == second.utterance_id


def test_command_started_during_name_final_decode_is_not_lost():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.observe(window(1, False), IDLE)
    assert policy.observe(window(2, True), IDLE) == []
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    (command,) = policy.observe(window(3, True), IDLE)
    assert command.onset_sample == 1024


def test_name_window_expires_without_capture_callbacks():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    policy.tick(10)
    assert policy.state == SessionState.ARMED


def test_repeated_kws_during_command_window_does_not_replace_or_extend_it():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.observe(window(1, False), IDLE)
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    deadline = policy.command_until
    assert policy.accept_wake("a", 1024, 0.064, IDLE) == []
    assert policy.command_until == deadline
    (command,) = policy.observe(window(2), IDLE)
    assert command.reason == "command_after_name"


def test_name_repeated_later_in_command_window_is_a_new_wake_not_a_command():
    policy = armed()
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.observe(window(1, False), IDLE)
    policy.finish_utterance(action.utterance_id, "wakeword_only")
    for n in range(2, 63):
        assert policy.observe(window(n, False), IDLE) == []
    (command,) = policy.observe(window(63), IDLE)
    assert command.reason == "command_after_name"
    rejected, wake = policy.accept_wake("a", 64 * 512, 64 * 0.032, IDLE)
    assert rejected.utterance_id == command.utterance_id
    assert rejected.reason == "superseded_by_wakeword"
    assert wake.reason == "acoustic_wakeword" and not policy.command_window_open


def waiting_for_reply(**kwargs):
    policy = armed(followup_window_seconds=8, echo_hold_seconds=0.1, **kwargs)
    (action,) = policy.accept_wake("a", 512, 0.032, IDLE)
    policy.finish_utterance(action.utterance_id, "final", 100)
    policy.observe(VadWindow("a", 512, 512, 1.0, 0.0, False), IDLE)
    return policy, action.utterance_id


def test_followup_is_after_completed_reply_and_actual_quiet_not_asr_final():
    policy, utterance_id = waiting_for_reply()
    assert policy.state == SessionState.ARMED
    assert policy.reply_finished("a", utterance_id, 100, 1.0, True)
    policy.tick(1.0, SPEAKING)
    assert not policy._awaiting_command
    policy.tick(1.1, IDLE)
    policy.tick(1.21, IDLE)
    assert policy._awaiting_command
    (command,) = policy.observe(VadWindow("a", 1024, 512, 1.3, 0.9, True), IDLE)
    assert command.reason == "followup_without_wakeword"
    policy.finish_utterance(command.utterance_id, "final", 1024)
    assert policy.reply_finished("a", command.utterance_id, 1024, 2.0, True)


@pytest.mark.parametrize("kind", ["aborted", "wrong_id", "wrong_session", "wrong_sample"])
def test_foreign_or_aborted_reply_does_not_open_followup(kind):
    policy, utterance_id = waiting_for_reply()
    accepted = policy.reply_finished(
        "b" if kind == "wrong_session" else "a",
        utterance_id + (kind == "wrong_id"),
        101 if kind == "wrong_sample" else 100,
        1.0,
        kind != "aborted",
    )
    assert not accepted
    policy.tick(1.0, IDLE)
    policy.tick(1.5, IDLE)
    assert not policy._awaiting_command


def test_followup_does_not_admit_speech_that_started_before_echo_hold_ended():
    policy, utterance_id = waiting_for_reply()
    policy.reply_finished("a", utterance_id, 100, 1.0, True)
    policy.tick(1.0, IDLE)
    policy.observe(VadWindow("a", 1024, 512, 1.05, 0.9, True), IDLE)
    policy.tick(1.2, IDLE)
    assert policy.observe(VadWindow("a", 1536, 512, 1.3, 0.9, True), IDLE) == []
    policy.observe(VadWindow("a", 2048, 512, 1.4, 0.0, False), IDLE)
    (command,) = policy.observe(VadWindow("a", 2560, 512, 1.5, 0.9, True), IDLE)
    assert command.reason == "followup_without_wakeword"


def test_followup_expires_duplicates_and_capture_reset_cannot_reopen():
    policy, utterance_id = waiting_for_reply()
    policy.reply_finished("a", utterance_id, 100, 1.0, True)
    policy.tick(1.0, IDLE)
    policy.tick(1.2, IDLE)
    assert not policy.reply_finished("a", utterance_id, 100, 1.3, True)
    policy.tick(10.0, IDLE)
    assert policy.state == SessionState.ARMED
    assert policy.observe(VadWindow("a", 1024, 512, 10.1, 0.9, True), IDLE) == []
    policy.observe(VadWindow("b", 0, 512, 10.2, 0.0, False), IDLE)
    assert not policy.reply_finished("a", utterance_id, 100, 10.3, True)


def test_explicit_name_in_followup_fences_segment_and_reopens_acoustic_input():
    policy, utterance_id = waiting_for_reply()
    policy.reply_finished("a", utterance_id, 100, 1.0, True)
    policy.tick(1.0, IDLE)
    policy.tick(1.2, IDLE)
    (current,) = policy.observe(VadWindow("a", 1024, 512, 1.3, 0.9, True), IDLE)
    reject, admit = policy.accept_wake("a", 1536, 1.332, IDLE)
    assert reject.utterance_id == current.utterance_id
    assert reject.decision == InputDecision.REJECT
    assert admit.reason == "acoustic_wakeword" and admit.decision == InputDecision.ADMIT


def test_named_barge_cancel_once_no_asr_admit_before_output_ack():
    policy = armed(
        wakeword_barge_in_enabled=True,
        allow_unvalidated_wakeword_barge_in=True,
        echo_hold_seconds=0.1,
    )
    (stop,) = policy.accept_wake("a", 512, 0.032, SPEAKING)
    assert stop.cancel_output and stop.decision == InputDecision.REJECT
    assert policy.state == SessionState.STOPPING_OUTPUT
    assert policy.accept_wake("a", 1024, 0.064, SPEAKING) == []
    assert policy.observe(window(1), SPEAKING) == []
    policy.tick(0.1, OutputSnapshot(speaking_fresh=True, playback_stopped=False))
    assert not policy._awaiting_command
    policy.tick(0.2, IDLE)
    policy.tick(0.31, IDLE)
    assert policy._awaiting_command
    policy.observe(VadWindow("a", 1024, 512, 0.32, 0.0, False), IDLE)
    (command,) = policy.observe(VadWindow("a", 1536, 512, 0.4, 0.9, True), IDLE)
    assert command.decision == InputDecision.ADMIT and not command.cancel_output
    assert command.reason == "command_after_name"


@pytest.mark.parametrize(
    "output",
    [
        OutputSnapshot(True, False, True, True),
        OutputSnapshot(True, True, False, True),
        OutputSnapshot(True, True, True, False),
    ],
)
def test_unvalidated_test_optin_does_not_bypass_output_guards(output):
    policy = armed(wakeword_barge_in_enabled=True, allow_unvalidated_wakeword_barge_in=True)
    assert policy.accept_wake("a", 512, 0.032, output) == []


def test_named_barge_requires_validation_or_explicit_experimental_optin():
    assert armed(wakeword_barge_in_enabled=True).accept_wake("a", 512, 0.032, SPEAKING) == []


def test_failed_stop_ack_closes_gate_and_late_reply_does_not_reopen_it():
    policy = armed(wakeword_barge_in_enabled=True, allow_unvalidated_wakeword_barge_in=True)
    policy.accept_wake("a", 512, 0.032, SPEAKING)
    policy.tick(3.0, SPEAKING)
    assert policy.state == SessionState.ARMED and not policy._awaiting_command


def test_prompt_listen_opens_window_without_name_after_output_quiet():
    policy = armed(followup_window_seconds=8, echo_hold_seconds=0.1)
    policy.observe(VadWindow("a", 512, 512, 0.5, 0.0, False), IDLE)
    assert policy.prompt_listen(1.0)
    policy.tick(1.0, SPEAKING)
    assert not policy._awaiting_command
    policy.tick(1.1, IDLE)
    policy.tick(1.21, IDLE)
    assert policy._awaiting_command and policy.state == SessionState.LISTENING
    (command,) = policy.observe(VadWindow("a", 1024, 512, 1.3, 0.9, True), IDLE)
    assert command.decision == InputDecision.ADMIT
    assert command.reason == "followup_without_wakeword"


def test_prompt_listen_is_idempotent_and_refused_while_utterance_open():
    policy = armed(followup_window_seconds=8, echo_hold_seconds=0.1)
    policy.observe(VadWindow("a", 512, 512, 0.5, 0.0, False), IDLE)
    assert policy.prompt_listen(1.0)
    assert policy.prompt_listen(1.05)  # уже ждёт тишины -- не перезаводит
    busy = armed(followup_window_seconds=8)
    busy.accept_wake("a", 512, 0.032, IDLE)
    assert not busy.prompt_listen(0.1)


def test_prompt_listen_refused_when_followup_disabled_or_no_capture():
    assert not armed(followup_window_seconds=0).prompt_listen(1.0)
    fresh = WakewordSessionPolicy(followup_window_seconds=8)
    fresh.activate()
    assert not fresh.prompt_listen(1.0)
