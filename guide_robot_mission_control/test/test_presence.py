"""Присутствие посетителя: чистая логика (без ROS) + сборка через реальный узел (design §9.2).

`_publish_presence` живёт на `create_timer` под `use_sim_time` -- таймер
сам по себе не тикает по стенным часам, а ждёт продвижения /clock. Поэтому
каждая проверка состояния после события -- это "опубликовать
свидетельство, дать реальное время на доставку DDS-сообщения
(`time.sleep`), продвинуть sim-часы минимум на один период публикации,
дождаться следующего /mission/presence".
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.msg import (
    MissionState,
    Presence,
    SpeakingStatus,
    Transcript,
    VoiceActivity,
    Wakeword,
)
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter

from guide_robot_mission_control.lib.qos import (
    QOS_ASR_TRANSCRIPT,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
    QOS_VAD,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_mission_control.presence import PresenceTracker, vad_evidence_allowed
from guide_robot_mission_control.presence_monitor_node import PresenceMonitorNode
from test.mocks.harness import MissionTestHarness, wait_until

_TICK_PERIOD_S = 0.06  # > 1/publish_rate_hz(20.0) из _make_node


# -- PresenceTracker: чистая логика, без ROS --------------------------------


def test_tracker_not_present_before_any_evidence() -> None:
    tracker = PresenceTracker(disengage_timeout_s=120.0)
    assert tracker.present(now_ns=0) is False
    assert tracker.seconds_since_evidence(now_ns=0) == float("inf")


def test_tracker_present_immediately_after_evidence() -> None:
    tracker = PresenceTracker(disengage_timeout_s=120.0)
    tracker.record_evidence(now_ns=0, source="wakeword")
    assert tracker.present(now_ns=0) is True
    assert tracker.last_source == "wakeword"


def test_tracker_disengages_exactly_at_timeout() -> None:
    tracker = PresenceTracker(disengage_timeout_s=2.0)
    tracker.record_evidence(now_ns=0, source="vad")
    assert tracker.present(now_ns=int(1.999e9)) is True
    assert tracker.present(now_ns=int(2.0e9)) is False


def test_tracker_seconds_since_evidence_tracks_elapsed() -> None:
    tracker = PresenceTracker(disengage_timeout_s=120.0)
    tracker.record_evidence(now_ns=0, source="asr_final")
    assert tracker.seconds_since_evidence(now_ns=int(5e9)) == pytest.approx(5.0)


def test_tracker_newer_evidence_updates_source() -> None:
    tracker = PresenceTracker(disengage_timeout_s=120.0)
    tracker.record_evidence(now_ns=0, source="wakeword")
    tracker.record_evidence(now_ns=int(1e9), source="vad")
    assert tracker.last_source == "vad"


def test_tracker_ignores_out_of_order_evidence() -> None:
    tracker = PresenceTracker(disengage_timeout_s=120.0)
    tracker.record_evidence(now_ns=int(5e9), source="wakeword")
    tracker.record_evidence(now_ns=int(1e9), source="vad")
    assert tracker.last_source == "wakeword"


@pytest.mark.parametrize(
    ("ignore_gate", "speaking", "since_ended", "tail_s", "expected"),
    [
        (False, True, None, 0.3, True),
        (True, True, None, 0.3, False),
        (True, False, None, 0.3, True),
        (True, False, 0.1, 0.3, False),
        (True, False, 0.3, 0.3, True),
        (True, False, 0.5, 0.3, True),
    ],
)
def test_vad_evidence_allowed_matrix(
    ignore_gate: bool, speaking: bool, since_ended: float | None, tail_s: float, expected: bool
) -> None:
    assert (
        vad_evidence_allowed(
            ignore_vad_while_speaking=ignore_gate,
            speaking=speaking,
            seconds_since_speaking_ended=since_ended,
            tts_tail_s=tail_s,
        )
        is expected
    )


# -- PresenceMonitorNode: сборка через ROS -----------------------------------


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _make_node(harness: MissionTestHarness, **overrides: object) -> PresenceMonitorNode:
    values: dict[str, object] = {"publish_rate_hz": 20.0}
    values.update(overrides)
    params = [Parameter("use_sim_time", value=True)]
    params.extend(Parameter(name, value=value) for name, value in values.items())
    node = PresenceMonitorNode(context=harness.context, parameter_overrides=params)
    harness.add_node(node)
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    return node


def _presence_listener(client_node) -> dict:
    state = {"latest": None, "count": 0}

    def _on_presence(msg: Presence) -> None:
        state["latest"] = msg
        state["count"] += 1

    client_node.create_subscription(
        Presence, "/mission/presence", _on_presence, QOS_MISSION_PRESENCE
    )
    return state


def _tick(harness: MissionTestHarness, state: dict, *, period_s: float = _TICK_PERIOD_S) -> None:
    before = state["count"]
    harness.clock.advance(period_s)
    wait_until(lambda: state["count"] > before, timeout_s=15.0)


def test_node_activates_and_publishes_without_any_evidence(harness: MissionTestHarness) -> None:
    """Заодно проверяет §6: отсутствие /perception/people (подписки на него нет) не мешает узлу."""
    _make_node(harness)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)

    _tick(harness, state)

    assert state["latest"] is not None
    assert state["latest"].present is False


def test_wakeword_above_threshold_sets_present(harness: MissionTestHarness) -> None:
    _make_node(harness, wakeword_min_confidence=0.6)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    wakeword_pub = client_node.create_publisher(Wakeword, "/speech/wakeword", QOS_WAKEWORD)

    wakeword_pub.publish(Wakeword(confidence=0.9, keyword="робот"))
    time.sleep(0.02)
    _tick(harness, state)

    assert state["latest"].present is True
    assert state["latest"].last_source == "wakeword"


def test_wakeword_below_threshold_ignored(harness: MissionTestHarness) -> None:
    _make_node(harness, wakeword_min_confidence=0.6)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    wakeword_pub = client_node.create_publisher(Wakeword, "/speech/wakeword", QOS_WAKEWORD)

    wakeword_pub.publish(Wakeword(confidence=0.3, keyword="робот"))
    time.sleep(0.02)
    _tick(harness, state)

    assert state["latest"].present is False


def test_asr_final_sets_present_but_partial_does_not(harness: MissionTestHarness) -> None:
    _make_node(harness)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    transcript_pub = client_node.create_publisher(
        Transcript, "/asr/transcript", QOS_ASR_TRANSCRIPT
    )

    transcript_pub.publish(Transcript(text="прив", is_final=False))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is False

    transcript_pub.publish(Transcript(text="привет", is_final=True))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is True
    assert state["latest"].last_source == "asr_final"


def test_vad_ignored_while_speaking_then_counted_after_tail(harness: MissionTestHarness) -> None:
    _make_node(harness, ignore_vad_while_speaking=True, tts_tail_ms=100.0)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    vad_pub = client_node.create_publisher(VoiceActivity, "/vad", QOS_VAD)
    speaking_pub = client_node.create_publisher(
        SpeakingStatus, "/voice/speaking", QOS_VOICE_SPEAKING
    )

    speaking_pub.publish(SpeakingStatus(speaking=True))
    time.sleep(0.02)
    vad_pub.publish(VoiceActivity(active=True, probability=0.9))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is False

    speaking_pub.publish(SpeakingStatus(speaking=False))
    time.sleep(0.02)
    # ещё внутри tts_tail_ms=100 мс sim-времени -- свидетельство должно быть отброшено.
    vad_pub.publish(VoiceActivity(active=True, probability=0.9))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is False

    harness.clock.advance(0.2)  # хвост истёк
    time.sleep(0.02)
    vad_pub.publish(VoiceActivity(active=True, probability=0.9))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is True
    assert state["latest"].last_source == "vad"


def test_vad_counted_even_while_speaking_when_gate_disabled(harness: MissionTestHarness) -> None:
    _make_node(harness, ignore_vad_while_speaking=False)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    vad_pub = client_node.create_publisher(VoiceActivity, "/vad", QOS_VAD)
    speaking_pub = client_node.create_publisher(
        SpeakingStatus, "/voice/speaking", QOS_VOICE_SPEAKING
    )

    speaking_pub.publish(SpeakingStatus(speaking=True))
    time.sleep(0.02)
    vad_pub.publish(VoiceActivity(active=True, probability=0.9))
    time.sleep(0.02)
    _tick(harness, state)

    assert state["latest"].present is True
    assert state["latest"].last_source == "vad"


def test_disengages_after_timeout(harness: MissionTestHarness) -> None:
    _make_node(harness, disengage_timeout_s=1.0, wakeword_min_confidence=0.6)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    wakeword_pub = client_node.create_publisher(Wakeword, "/speech/wakeword", QOS_WAKEWORD)

    wakeword_pub.publish(Wakeword(confidence=0.9, keyword="робот"))
    time.sleep(0.02)
    _tick(harness, state)
    assert state["latest"].present is True

    # _tick сама продвигает часы на _TICK_PERIOD_S -- это тоже часть
    # бюджета до disengage_timeout_s=1.0, поэтому берём период поменьше,
    # чтобы не перепрыгнуть порог раньше срока.
    harness.clock.advance(0.8)
    _tick(harness, state, period_s=0.05)
    assert state["latest"].present is True  # суммарно ~0.86 с < 1.0

    harness.clock.advance(0.2)  # суммарно ~1.06 с > disengage_timeout_s=1.0
    _tick(harness, state)
    assert state["latest"].present is False
    assert state["latest"].seconds_since_evidence >= 1.0


def test_mission_state_weak_evidence_when_enabled(harness: MissionTestHarness) -> None:
    _make_node(harness, mission_state_weak_evidence=True)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    mission_state_pub = client_node.create_publisher(
        MissionState, "/mission/state", QOS_MISSION_STATE
    )

    mission_state_pub.publish(MissionState(stop_index=1))
    time.sleep(0.02)
    _tick(harness, state)

    assert state["latest"].present is True
    assert state["latest"].last_source == "mission_state"


def test_mission_state_ignored_by_default(harness: MissionTestHarness) -> None:
    _make_node(harness)
    client_node = harness.make_client_node()
    state = _presence_listener(client_node)
    mission_state_pub = client_node.create_publisher(
        MissionState, "/mission/state", QOS_MISSION_STATE
    )

    mission_state_pub.publish(MissionState(stop_index=1))
    time.sleep(0.02)
    _tick(harness, state)

    assert state["latest"].present is False
