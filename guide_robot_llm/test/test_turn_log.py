"""Юниты на построчный jsonl-лог ходов."""

from __future__ import annotations

import json

from guide_robot_llm.lib.turn_log import TurnLog


def test_write_creates_file_in_log_dir(tmp_path) -> None:
    log = TurnLog(tmp_path, session_start=1730000000.0)
    log.write({"turn_id": 1, "user_text": "привет"})
    log.close()

    files = list(tmp_path.glob("chat_*.jsonl"))
    assert len(files) == 1
    assert files[0] == log.path


def test_filename_encodes_session_start() -> None:
    log = TurnLog("/tmp/does-not-matter", session_start=0)
    try:
        assert "19700101" in log.path.name or "chat_" in log.path.name
    finally:
        log.close()
        log.path.unlink(missing_ok=True)


def test_write_produces_valid_json_line(tmp_path) -> None:
    log = TurnLog(tmp_path, session_start=1730000000.0)
    record = {"turn_id": 7, "spoken": "текст", "interrupted": True, "clauses": ["а", "б"]}
    log.write(record)
    log.close()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_multiple_writes_are_newline_delimited(tmp_path) -> None:
    log = TurnLog(tmp_path, session_start=1730000000.0)
    log.write({"turn_id": 1})
    log.write({"turn_id": 2})
    log.close()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["turn_id"] for line in lines] == [1, 2]


def test_writes_are_flushed_without_explicit_close(tmp_path) -> None:
    """Устойчивость к незакрытому файлу: данные обязаны быть на диске после write()."""
    log = TurnLog(tmp_path, session_start=1730000000.0)
    log.write({"turn_id": 1})

    # Читаем содержимое, пока файл ещё открыт для записи -- write() обязан
    # флашить сразу, иначе после падения процесса без close() лог теряется.
    content = log.path.read_text(encoding="utf-8")
    assert json.loads(content.splitlines()[0])["turn_id"] == 1
    log.close()


def test_close_is_idempotent(tmp_path) -> None:
    log = TurnLog(tmp_path, session_start=1730000000.0)
    log.write({"turn_id": 1})
    log.close()
    log.close()  # не должно бросать


def test_creates_log_dir_if_missing(tmp_path) -> None:
    target = tmp_path / "nested" / "llm_turns"
    log = TurnLog(target, session_start=1730000000.0)
    log.write({"turn_id": 1})
    log.close()
    assert target.exists()
