"""Результат реального dialog/tool/Say пути адресован исходной ASR-реплике."""

import json

import pytest

from guide_robot_msgs.msg import CancelAll, DialogTurnEvent, Transcript
from test.mocks.harness import ToolBrokerTestHarness, wait_until
from test.mocks.mock_llm_server import MockLlmServer


def _client(harness, events):
    client = harness.make_client_node()
    client.create_subscription(DialogTurnEvent, "/dialog/turn_event", events.append, 10)
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    wait_until(lambda: pub.get_subscription_count() >= 1)
    wait_until(lambda: client.count_publishers("/dialog/turn_event") >= 1)
    return client, pub


def _transcript(utterance_id, start_sample, text):
    return Transcript(
        device_session_id="capture-1",
        utterance_id=utterance_id,
        start_sample=start_sample,
        wakeword_admitted=True,
        is_final=True,
        text=text,
    )


@pytest.mark.parametrize(
    "say_fails,reply,expects_reply",
    [
        (False, "Привет!", False),
        (True, "Привет!", False),
        (False, "Рассказать про город или про университет?", True),
        (True, "Рассказать про город или про университет?", False),
    ],
)
def test_turn_result_follows_actual_say_result(say_fails, reply, expects_reply):
    harness = ToolBrokerTestHarness()
    try:
        wait_until(lambda: harness.dialog_agent.last_mission_state() is not None)
        harness.llm_server.chunks_with_grammar = [json.dumps({"tool": "reply", "args": {}})]
        harness.llm_server.chunks_no_grammar = [reply]
        if say_fails:
            harness.say.fail_on_text.add(reply)
        events = []
        _, pub = _client(harness, events)
        pub.publish(_transcript(42, 12345, "расскажи о себе"))
        wait_until(lambda: len(events) == 1)
        event = events[0]
        assert (event.device_session_id, event.utterance_id, event.start_sample) == (
            "capture-1",
            42,
            12345,
        )
        assert event.dialog_session_id == harness.dialog_agent._session_id
        assert event.turn_id > 0
        assert event.reply_completed is not say_fails
        # Окно без имени открывает только озвученный вопрос, не любой ответ.
        assert event.expects_reply is expects_reply
        assert harness.say.goals_received == 1
    finally:
        harness.shutdown()


def test_cancelled_turn_cannot_rearm_followup():
    harness = ToolBrokerTestHarness()
    try:
        wait_until(lambda: harness.dialog_agent.last_mission_state() is not None)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        harness.llm_server.chunks_with_grammar = [
            '{"tool":',
            '"reply",',
            '"args": {}}',
        ]
        harness.llm_server.chunk_delay_s = 0.3
        events = []
        client, pub = _client(harness, events)
        cancel = client.create_publisher(CancelAll, "/speech/cancel_all", 10)
        wait_until(lambda: cancel.get_subscription_count() >= 1)
        pub.publish(_transcript(42, 12345, "расскажи длинную историю"))
        wait_until(lambda: harness.llm_server.last_request_body is not None)
        cancel.publish(CancelAll(reason=CancelAll.REASON_BARGE_IN))
        wait_until(lambda: len(events) == 1)
        assert events[0].utterance_id == 42
        assert not events[0].reply_completed
        assert harness.say.goals_received == 0
    finally:
        harness.shutdown()


def test_pending_replay_preserves_new_voice_origin():
    harness = ToolBrokerTestHarness()
    try:
        wait_until(lambda: harness.dialog_agent.last_mission_state() is not None)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        harness.llm_server.chunks_with_grammar = [
            '{"tool":',
            '"reply",',
            '"args": {}}',
        ]
        harness.llm_server.chunks_no_grammar = ["Ответ."]
        harness.llm_server.chunk_delay_s = 0.3
        events = []
        _, pub = _client(harness, events)
        pub.publish(_transcript(42, 12345, "первая реплика"))
        wait_until(lambda: harness.llm_server.last_request_body is not None)
        harness.llm_server.mode = MockLlmServer.MODE_OK
        pub.publish(_transcript(43, 54321, "вторая реплика"))
        wait_until(lambda: len(events) == 2, timeout_s=10)
        assert [(e.utterance_id, e.start_sample, e.reply_completed) for e in events] == [
            (42, 12345, False),
            (43, 54321, True),
        ]
        assert all(e.device_session_id == "capture-1" for e in events)
    finally:
        harness.shutdown()
