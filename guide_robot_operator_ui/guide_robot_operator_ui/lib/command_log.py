"""Построчный jsonl-лог попыток входа и команд (design E6).

Копия по духу `guide_robot_llm/guide_robot_llm/lib/interaction_sink.py`:
flush() на каждую строку, не буферизация -- лог нужен для разбора
инцидентов, и падение процесса не должно стоить последних записей.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TextIO

__all__ = ["CommandLogSink"]


class CommandLogSink:
    """Пишет по одной jsonl-строке в `log_dir/operator_ui_YYYYmmdd_HHMMSS.jsonl`."""

    def __init__(self, log_dir: str | Path, session_start: float | None = None) -> None:
        """Открыть файл лога сессии узла. Каталог создаётся при отсутствии."""
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        started_at = session_start if session_start is not None else time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started_at))
        self._path = directory / f"operator_ui_{stamp}.jsonl"
        self._file: TextIO = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        """Путь к файлу текущей сессии узла."""
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        """Записать одну строку и сразу сбросить буфер на диск."""
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        """Закрыть файл. Безопасно вызывать повторно."""
        if not self._file.closed:
            self._file.close()
