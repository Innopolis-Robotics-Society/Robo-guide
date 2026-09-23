"""Юниты на построчный jsonl-журнал."""

from __future__ import annotations

import json

from guide_launcher.journal import Journal


def _records(journal: Journal) -> list[dict]:
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]


def test_write_creates_launcher_file_in_log_dir(tmp_path) -> None:
    journal = Journal(tmp_path, session_start=1730000000.0)
    journal.write("command", path="/api/tour/stop")
    journal.close()

    files = list(tmp_path.glob("launcher_*.jsonl"))
    assert files == [journal.path]


def test_write_produces_valid_json_line_with_ts_and_event(tmp_path) -> None:
    journal = Journal(tmp_path, session_start=1730000000.0)
    journal.write("auth_attempt", operator="pin", ok=True)
    journal.close()

    (record,) = _records(journal)
    assert record["event"] == "auth_attempt"
    assert record["operator"] == "pin"
    assert record["ok"] is True
    assert isinstance(record["ts"], float)


def test_multiple_writes_are_newline_delimited(tmp_path) -> None:
    journal = Journal(tmp_path, session_start=1730000000.0)
    journal.write("command", n=1)
    journal.write("command", n=2)
    journal.close()

    assert [r["n"] for r in _records(journal)] == [1, 2]


def test_writes_are_flushed_without_explicit_close(tmp_path) -> None:
    journal = Journal(tmp_path, session_start=1730000000.0)
    journal.write("command")

    assert _records(journal)[0]["event"] == "command"
    journal.close()


def test_close_is_idempotent_and_write_after_close_is_ignored(tmp_path) -> None:
    journal = Journal(tmp_path, session_start=1730000000.0)
    journal.write("command")
    journal.close()
    journal.close()
    journal.write("late")
    assert len(_records(journal)) == 1


def test_creates_log_dir_if_missing(tmp_path) -> None:
    target = tmp_path / "nested" / "launcher"
    journal = Journal(target, session_start=1730000000.0)
    journal.write("command")
    journal.close()
    assert target.exists()
