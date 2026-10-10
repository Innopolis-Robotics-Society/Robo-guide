"""ROS boundaries без микрофона, динамиков, сети и тяжёлого ASR."""

from types import SimpleNamespace

import pytest
import rclpy
from guide_robot_msgs.msg import (
    AudioChunk,
    DialogTurnEvent,
    PlaybackState,
    SpeakingStatus,
    Transcript,
    UtteranceControl,
    UtteranceEvent,
    VoiceActivity,
    WakewordCandidate,
)
from rclpy.parameter import Parameter

from guide_robot_voice.asr_node import AsrNode
from guide_robot_voice.lib.ring import IndexedAudioRing, RingBuffer
from guide_robot_voice.lib.session_policy import OutputSnapshot, VadWindow
from guide_robot_voice.lib.turn_policy import TurnPolicy
from guide_robot_voice.lib.wakeword_session_policy import WakewordSessionPolicy
from guide_robot_voice.voice_session_manager import VoiceSessionManager
from guide_robot_voice.wakeword_node import WakewordNode


class Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


@pytest.fixture
def ros():
    rclpy.init()
    nodes = []
    yield nodes
    for node in nodes:
        node.destroy_node()
    rclpy.try_shutdown()


def statuses(node, speaking=False):
    status = SpeakingStatus()
    status.stamp = node.get_clock().now().to_msg()
    status.speaking = speaking
    status.interruptible = True
    node._latest_speaking = status
    playback = PlaybackState()
    playback.stamp = node.get_clock().now().to_msg()
    playback.device_session_id = "a"
    playback.state = PlaybackState.STATE_PLAYING if speaking else PlaybackState.STATE_IDLE
    node._latest_playback = playback


def manager(ros):
    node = VoiceSessionManager()
    ros.append(node)
    node.set_parameters(
        [
            Parameter("wakeword_gated", value=True),
            Parameter("wakeword_model_sha256", value="frozen"),
        ]
    )
    node._active = True
    node._policy = WakewordSessionPolicy()
    node._policy.activate()
    now = node.get_clock().now().nanoseconds / 1e9
    node._policy.observe(
        VadWindow("a", 0, 512, now, 0.9, True), OutputSnapshot(speaking_fresh=True)
    )
    for name in ("_control_pub", "_state_pub", "_wakeword_pub", "_cancel_pub"):
        setattr(node, name, Publisher())
    statuses(node)
    return node


def candidate(node):
    msg = WakewordCandidate()
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.device_session_id = "a"
    msg.end_sample = 512
    msg.keyword = "фирая"
    msg.confidence = 0.9
    msg.model_sha256 = "frozen"
    return msg


@pytest.mark.parametrize(
    "kind",
    ["stale", "future", "wrong_model", "wrong_session", "low_score", "nan", "stale_output", "tts"],
)
def test_rejected_candidate_cannot_publish_wake_admission_or_cancel(ros, kind):
    node = manager(ros)
    msg = candidate(node)
    if kind == "stale":
        msg.header.stamp.sec -= 10
    elif kind == "future":
        msg.header.stamp.sec += 10
    elif kind == "wrong_model":
        msg.model_sha256 = "other"
    elif kind == "wrong_session":
        msg.device_session_id = "b"
    elif kind == "low_score":
        msg.confidence = 0.1
    elif kind == "nan":
        msg.confidence = float("nan")
    elif kind == "stale_output":
        node._latest_playback.stamp.sec -= 10
    elif kind == "tts":
        statuses(node, True)
    node._on_wakeword_candidate(msg)
    assert node._wakeword_pub.messages == []
    assert node._control_pub.messages == []
    assert node._cancel_pub.messages == []


def test_accepted_candidate_is_one_wake_and_admission_no_idle_cancel(ros):
    node = manager(ros)
    msg = candidate(node)
    node._on_wakeword_candidate(msg)
    node._on_wakeword_candidate(msg)
    assert len(node._wakeword_pub.messages) == len(node._control_pub.messages) == 1
    assert node._control_pub.messages[0].onset_sample == 512
    assert node._control_pub.messages[0].decision == UtteranceControl.DECISION_ADMIT
    assert node._cancel_pub.messages == []


def asr(ros, text):
    node = AsrNode()
    ros.append(node)
    node.set_parameters(
        [
            Parameter("session_managed_input", value=True),
            Parameter("wakeword_gated_input", value=True),
            Parameter("pre_roll_ms", value=1200.0),
        ]
    )
    node._is_active = True
    node._pre_roll = RingBuffer(16000, max_samples=19200)
    node._indexed_pre_roll = IndexedAudioRing(16000, max_samples=35200)
    for name in ("_utterance_event_pub", "_transcript_pub", "_partial_pub", "_kws_partial_pub"):
        setattr(node, name, Publisher())
    node.calls = []

    def decode(pcm):
        node.calls.append(pcm.copy())
        return SimpleNamespace(text=text, confidence=0.9)

    node._asr = SimpleNamespace(decode=decode)
    return node


def fill_audio(node):
    for first in range(0, 32000, 256):
        chunk = AudioChunk()
        chunk.header.stamp = node.get_clock().now().to_msg()
        chunk.device_session_id = "a"
        chunk.sample_rate = 16000
        chunk.channels = 1
        chunk.first_sample = first
        chunk.data = list(range(first, first + 256))
        node._on_audio(chunk)


def control(decision=UtteranceControl.DECISION_ADMIT, sequence=1):
    msg = UtteranceControl()
    msg.device_session_id = "a"
    msg.utterance_id = 1
    msg.onset_sample = 32000
    msg.control_sequence = sequence
    msg.decision = decision
    return msg


def test_idle_asr_keeps_ring_but_never_decodes_even_kws_only(ros):
    node = asr(ros, "background")
    fill_audio(node)
    node._submit_decode("partial")
    node._submit_decode("final")
    node._on_input_control(control(UtteranceControl.DECISION_KWS_ONLY))
    node._submit_decode("partial")
    assert not node._utterance_open
    assert node._decode_jobs.empty() and node.calls == []


@pytest.mark.parametrize(
    "text,only_name", [("Фирая!", True), ("фи рая", True), ("расскажи о картине", False)]
)
def test_final_has_exact_preroll_and_only_name_does_not_enter_dialog(ros, text, only_name):
    node = asr(ros, text)
    fill_audio(node)
    node._on_input_control(control())
    assert node._utterance_start_sample == 12800 and node._utterance_samples == 19200
    node._submit_decode("final")
    node._run_decode(node._decode_jobs.get_nowait())
    assert len(node.calls) == 1
    assert node.calls[0][0] == 12800 and node.calls[0][-1] == 31999
    assert node._utterance_event_pub.messages[-1].event == UtteranceEvent.EVENT_FINAL
    if only_name:
        assert node._transcript_pub.messages == []
        assert node._utterance_event_pub.messages[-1].status == "wakeword_only"
    else:
        assert node._transcript_pub.messages[0].wakeword_admitted is True


def test_reject_fences_queued_decode_before_heavy_model_call(ros):
    node = asr(ros, "do not publish")
    fill_audio(node)
    node._on_input_control(control())
    node._submit_decode("final")
    job = node._decode_jobs.get_nowait()
    node._on_input_control(control(UtteranceControl.DECISION_REJECT, 2))
    node._run_decode(job)
    assert node.calls == [] and node._transcript_pub.messages == []


def test_oww_emits_candidate_and_asr_name_does_not_retrigger(ros, monkeypatch):
    import guide_robot_voice.wakeword_node as module

    class FakeModel:
        def __init__(self, _path):
            self.manifest = {
                "inference_config": {
                    "threshold": 0.8,
                    "consecutive_frames": 3,
                    "cooldown_seconds": 2,
                },
                "sha256": {"firaya_trial.onnx": "frozen"},
            }

        def reset(self):
            pass

        def score(self, _pcm):
            return 0.9

    monkeypatch.setattr(module, "FrozenWakewordModel", FakeModel)
    node = WakewordNode()
    ros.append(node)
    node.set_parameters(
        [Parameter("backend", value="oww"), Parameter("model_bundle_path", value="/unused")]
    )
    node._configure()
    node._is_active = True
    node._candidate_pub = Publisher()
    node._wakeword_pub = Publisher()
    node._cancel_pub = Publisher()
    msg = AudioChunk()
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.device_session_id = "a"
    msg.sample_rate = 16000
    msg.channels = 1
    msg.data = [0] * 3840
    node._on_audio(msg)
    assert len(node._candidate_pub.messages) == 1
    assert node._candidate_pub.messages[0].end_sample == 3840
    transcript = Transcript()
    transcript.text = "фирая расскажи о картине"
    node._on_partial(transcript)
    assert node._wakeword_pub.messages == [] and node._cancel_pub.messages == []


@pytest.mark.parametrize("text", ["", "сырая", "фрая", "ферая", "фрея", "фера я"])
def test_actual_wake_fragment_preserves_window_regardless_of_asr_spelling(ros, text):
    node = asr(ros, text)
    fill_audio(node)
    command = control()
    command.reason = "acoustic_wakeword"
    node._on_input_control(command)
    node._asr = SimpleNamespace(
        decode=lambda pcm: SimpleNamespace(
            text=text,
            confidence=-1.0,
            tokens=tuple(text),
            timestamps=tuple(0.4 + i * 0.04 for i in range(len(text))),
        )
    )
    node._submit_decode("final")
    node._run_decode(node._decode_jobs.get_nowait())
    event = node._utterance_event_pub.messages[-1]
    assert event.status == "wakeword_only"
    assert node._transcript_pub.messages == []
    policy = WakewordSessionPolicy()
    policy.activate()
    output = OutputSnapshot(speaking_fresh=True)
    policy.observe(VadWindow("a", 0, 512, 0.0, 0.9, True), output)
    (first,) = policy.accept_wake("a", 512, 0.032, output)
    policy.observe(VadWindow("a", 512, 512, 0.032, 0.0, False), output)
    policy.finish_utterance(first.utterance_id, event.status)
    (second,) = policy.observe(VadWindow("a", 1024, 512, 2.0, 0.9, True), output)
    assert second.reason == "command_after_name"


def test_command_after_name_uses_short_preroll_and_one_word_command_is_not_suppressed(ros):
    node = asr(ros, "привет")
    fill_audio(node)
    command = control()
    command.reason = "command_after_name"
    node._on_input_control(command)
    assert node._prefix_samples == 2560
    assert node._utterance_start_sample == 29440
    node._submit_decode("final")
    job = node._decode_jobs.get_nowait()
    assert job.reason == "command_after_name"
    node._run_decode(job)
    assert node._transcript_pub.messages[0].text == "привет"
    assert node._transcript_pub.messages[0].wakeword_admitted is True


def test_vad_silence_before_wake_cannot_finalize_new_segment(ros):
    node = asr(ros, "")
    fill_audio(node)
    node._turn_policy = TurnPolicy()
    command = control()
    command.reason = "acoustic_wakeword"
    node._on_input_control(command)
    onset = node._utterance_timestamp + node._prefix_samples / 16000
    msg = VoiceActivity()
    nanoseconds = round((onset - 1) * 1e9)
    msg.header.stamp.sec = nanoseconds // 1_000_000_000
    msg.header.stamp.nanosec = nanoseconds % 1_000_000_000
    msg.active = False
    msg.state_duration = 10.0
    node._on_vad(msg)
    assert node._utterance_open
    assert node._decode_jobs.empty()


def test_followup_wake_text_cannot_extend_command_window_again(ros):
    node = asr(ros, "фирая")
    fill_audio(node)
    command = control()
    command.reason = "command_after_name"
    node._on_input_control(command)
    node._submit_decode("final")
    node._run_decode(node._decode_jobs.get_nowait())
    assert node._utterance_event_pub.messages[-1].status == "final"


@pytest.mark.parametrize("kind", ["valid", "stale", "aborted", "wrong_capture", "wrong_sample"])
def test_dialog_result_is_correlated_and_stale_results_cannot_rearm(ros, kind):
    node = manager(ros)
    node._policy = WakewordSessionPolicy(followup_window_seconds=8, echo_hold_seconds=0.0)
    node._policy.activate()
    now = node.get_clock().now().nanoseconds / 1e9
    node._policy.observe(
        VadWindow("a", 0, 512, now, 0.9, True),
        OutputSnapshot(speaking_fresh=True, playback_stopped=True),
    )
    node._on_wakeword_candidate(candidate(node))
    final = UtteranceEvent(
        device_session_id="a",
        utterance_id=1,
        start_sample=123,
        event=UtteranceEvent.EVENT_FINAL,
        status="final",
    )
    node._on_utterance_event(final)
    event = DialogTurnEvent(
        device_session_id="a", utterance_id=1, start_sample=123, reply_completed=True
    )
    event.stamp = node.get_clock().now().to_msg()
    if kind == "stale":
        event.stamp.sec -= 10
    if kind == "aborted":
        event.reply_completed = False
    if kind == "wrong_capture":
        event.device_session_id = "other"
    if kind == "wrong_sample":
        event.start_sample += 1
    node._on_dialog_turn(event)
    assert node._policy._awaiting_command == (kind == "valid")


def test_kws_during_tts_cancels_output_but_does_not_admit_asr_echo(ros):
    node = manager(ros)
    node._policy.wakeword_barge_in_enabled = True
    node._policy.allow_unvalidated_wakeword_barge_in = True
    statuses(node, True)
    node._on_wakeword_candidate(candidate(node))
    assert len(node._cancel_pub.messages) == 1
    assert node._control_pub.messages[0].decision == UtteranceControl.DECISION_REJECT
    assert node._wakeword_pub.messages[0].tts_active
    node._on_wakeword_candidate(candidate(node))
    assert len(node._cancel_pub.messages) == 1


def test_followup_asr_keeps_single_word_and_capture_metadata(ros):
    node = asr(ros, "да")
    fill_audio(node)
    request = control()
    request.reason = "followup_without_wakeword"
    node._on_input_control(request)
    assert node._prefix_samples == 2560
    node._submit_decode("final")
    node._run_decode(node._decode_jobs.get_nowait())
    final = node._transcript_pub.messages[0]
    assert final.text == "да" and final.wakeword_admitted
    assert final.device_session_id == "a" and final.start_sample == 29440
