"""interaction_log end-to-end: ход через dialog_agent -> jsonl на диске (план §8)."""

from __future__ import annotations

import json

from guide_robot_msgs.msg import Transcript
from test.mocks.harness import ToolBrokerTestHarness, wait_until


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def _log_lines(harness: ToolBrokerTestHarness) -> list[dict]:
    path = harness.interaction_log._sink.path  # noqa: SLF001 -- тестовая интроспекция
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_turn_produces_interaction_log_record() -> None:
    harness = ToolBrokerTestHarness()
    try:
        wait_until(lambda: harness.dialog_agent.last_mission_state() is not None, timeout_s=5.0)
        harness.llm_server.chunks_no_grammar = ["Привет!"]
        harness.llm_server.chunks_with_grammar = ['{"tool": "noop", "args": {}}']

        client = harness.make_client_node()
        _publish_transcript(client, "робот, привет")

        wait_until(lambda: len(_log_lines(harness)) >= 1, timeout_s=5.0)
        lines = _log_lines(harness)

        assert len(lines) == 1
        record = lines[0]
        assert record["schema_version"] == 4
        assert record["utterance"] == "привет"
        assert isinstance(record["session_id"], str) and record["session_id"]
        assert record["utterance_ts"] > 0
        assert record["answer_text"] == "Привет!"
        assert record["say_ok"] is True
        assert record["stopped_reason"] == "ok"
        assert record["degraded"] is False
        assert record["action"] == {
            "tool": "noop",
            "args": {},
            "think": "",
            "ok": True,
            "message": "",
            "content_version": None,
        }
        assert record["repair_used"] is False
        assert record["references"] == []
        assert record["verbatim_overlap_words"] == 0
        assert record["history_entries"] >= 2  # реплика посетителя + реплика робота
        assert isinstance(record["told_ids"], list)
        assert any(t["stage"] == "llm_answer" for t in record["stage_timings"])
        assert any(t["stage"] == "say" for t in record["stage_timings"])
        assert any(t["stage"] == "llm_action" for t in record["stage_timings"])
        assert record["total_ms"] > 0
        # Сырой ввод/вывод ЛЛМ -- по запросу: весь обмен виден целиком, не
        # только то, что дошло до озвучки/действия.
        assert record["answer_raw_text"] == "Привет!"
        assert record["answer_finish_reason"] == "stop"
        assert record["action_raw_text"] == '{"tool": "noop", "args": {}}'
        assert record["action_finish_reason"] == "stop"
        assert record["llm_messages"][0]["role"] == "system"
        roles = [m["role"] for m in record["llm_messages"]]
        assert roles.count("assistant") == 2  # tool-call фазы действия + реплика
    finally:
        harness.shutdown()
