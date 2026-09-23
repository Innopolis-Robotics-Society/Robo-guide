"""Юниты на построчный jsonl-лог команд/попыток входа (design E6)."""

from __future__ import annotations

import json

from guide_robot_operator_ui.lib.command_log import CommandLogSink


def test_write_creates_file_in_log_dir(tmp_path) -> None:
    log = CommandLogSink(tmp_path, session_start=1730000000.0)
    log.write({"event": "command", "path": "/api/tour/stop"})
    log.close()

    files = list(tmp_path.glob("operator_ui_*.jsonl"))
    assert len(files) == 1
    assert files[0] == log.path


def test_write_produces_valid_json_line(tmp_path) -> None:
    log = CommandLogSink(tmp_path, session_start=1730000000.0)
    record = {"event": "auth_attempt", "operator": "pin", "ok": True}
    log.write(record)
    log.close()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_multiple_writes_are_newline_delimited(tmp_path) -> None:
    log = CommandLogSink(tmp_path, session_start=1730000000.0)
    log.write({"event": "command", "n": 1})
    log.write({"event": "command", "n": 2})
    log.close()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_writes_are_flushed_without_explicit_close(tmp_path) -> None:
    """Устойчивость к незакрытому файлу: данные обязаны быть на диске после write()."""
    log = CommandLogSink(tmp_path, session_start=1730000000.0)
    log.write({"event": "command"})

    content = log.path.read_text(encoding="utf-8")
    assert json.loads(content.splitlines()[0])["event"] == "command"
    log.close()


def test_close_is_idempotent(tmp_path) -> None:
    log = CommandLogSink(tmp_path, session_start=1730000000.0)
    log.write({"event": "command"})
    log.close()
    log.close()  # не должно бросать


def test_creates_log_dir_if_missing(tmp_path) -> None:
    target = tmp_path / "nested" / "operator_ui"
    log = CommandLogSink(target, session_start=1730000000.0)
    log.write({"event": "command"})
    log.close()
    assert target.exists()
