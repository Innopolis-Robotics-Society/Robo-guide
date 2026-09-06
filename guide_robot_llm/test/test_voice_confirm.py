"""Голосовое закрытие AWAITING_CONFIRM/ANSWERING мимо ЛЛМ (llm_plam.md §3/§9).

tool_broker подписан на /asr/transcript сам -- тест публикует финалы,
как реальный ASR, и не дёргает call_tool("confirm"/"finish_answer", ...)
напрямую. Это и есть критерий готовности шага 3.
"""

from __future__ import annotations

import time

from guide_robot_msgs.msg import CancelAll, MissionState, Transcript
from test.mocks.harness import ToolBrokerTestHarness, pump_clock, wait_until

_S = MissionState


def _mission_state_is(harness: ToolBrokerTestHarness, target: int):
    def _predicate() -> bool:
        state = harness.broker.last_mission_state()
        return state is not None and state.state == target

    return _predicate


def _setup_two_stop_tour(harness: ToolBrokerTestHarness) -> None:
    for i, stop_id in enumerate(("stop0", "stop1")):
        harness.fixtures.add_exhibit(stop_id, [f"{stop_id} ч0.", f"{stop_id} ч1."], version="r1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = 0.05
    harness.say.chars_per_sec = 50.0


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def test_confirm_yes_via_voice_advances_to_next_stop() -> None:
    harness = ToolBrokerTestHarness()
    try:
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool(
            "tour_by_points", {"location_ids": ["stop0", "stop1"]}
        )
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)

        client = harness.make_client_node()
        _publish_transcript(client, "да, давайте")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
    finally:
        harness.shutdown()


def test_confirm_no_via_voice_ends_tour() -> None:
    harness = ToolBrokerTestHarness()
    try:
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool(
            "tour_by_points", {"location_ids": ["stop0", "stop1"]}
        )
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)

        client = harness.make_client_node()
        _publish_transcript(client, "нет, хватит")

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_IDLE), step=0.2)
    finally:
        harness.shutdown()


def test_confirm_unsure_text_does_not_advance() -> None:
    """Неуверенная фраза -- локальный матчер молчит, состояние не меняется (ждём ЛЛМ)."""
    harness = ToolBrokerTestHarness()
    try:
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool(
            "tour_by_points", {"location_ids": ["stop0", "stop1"]}
        )
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)

        client = harness.make_client_node()
        _publish_transcript(client, "а что это за экспонат вообще был")
        time.sleep(0.2)
        assert harness.broker.last_mission_state().state == _S.STATE_AWAITING_CONFIRM
    finally:
        harness.shutdown()


def test_answering_stop_phrase_via_voice_skips_current_stop() -> None:
    harness = ToolBrokerTestHarness()
    try:
        _setup_two_stop_tour(harness)
        harness.say.chars_per_sec = 10.0
        started = harness.broker.call_tool(
            "tour_by_points", {"location_ids": ["stop0", "stop1"]}
        )
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_NARRATING), step=0.1)

        client = harness.make_client_node()
        cancel_pub = client.create_publisher(CancelAll, "/speech/cancel_all", 1)
        cancel_pub.publish(
            CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
        )
        wait_until(_mission_state_is(harness, _S.STATE_ANSWERING), timeout_s=5.0)

        _publish_transcript(client, "хватит, дальше")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
    finally:
        harness.shutdown()
