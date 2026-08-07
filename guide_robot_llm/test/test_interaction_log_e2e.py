"""interaction_log end-to-end: ход через dialog_agent -> jsonl на диске (llm_plam.md §6)."""

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
        harness.llm_server.chunks = ['{"tool": "say", "args": {"text": "Привет!"}}']

        client = harness.make_client_node()
        _publish_transcript(client, "привет")

        wait_until(lambda: len(_log_lines(harness)) >= 1, timeout_s=5.0)
        lines = _log_lines(harness)

        assert len(lines) == 1
        record = lines[0]
        assert record["utterance"] == "привет"
        assert record["stopped_reason"] == "terminal_tool"
        assert record["degraded"] is False
        assert [c["tool"] for c in record["calls"]] == ["say"]
        assert record["calls"][0]["ok"] is True
        assert record["calls"][0]["content_version"] is None
        assert any(t["stage"] == "llm_call" for t in record["stage_timings"])
        assert any(t["stage"] == "tool_call" for t in record["stage_timings"])
        assert record["total_ms"] > 0
    finally:
        harness.shutdown()
