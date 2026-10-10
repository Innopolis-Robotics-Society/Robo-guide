"""Построчный jsonl-журнал входов, команд, публичных стартов и действий со стеком.

flush() на каждую строку: журнал нужен для разбора инцидентов, и падение
процесса не должно стоить последних записей.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, TextIO

__all__ = ["Journal"]


class Journal:
    """Пишет по одной jsonl-строке в `log_dir/launcher_YYYYmmdd_HHMMSS.jsonl`."""

    def __init__(self, log_dir: str | Path, session_start: float | None = None) -> None:
        """Открыть файл журнала запуска launcher. Каталог создаётся при отсутствии."""
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        started_at = session_start if session_start is not None else time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started_at))
        self._path = directory / f"launcher_{stamp}.jsonl"
        self._file: TextIO = self._path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Путь к файлу текущего запуска."""
        return self._path

    def write(self, event: str, **fields: Any) -> None:
        """Записать событие `event` с полями `fields` и меткой времени, сразу на диск."""
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            if self._file.closed:
                return
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        """Закрыть файл. Безопасно вызывать повторно."""
        with self._lock:
            if not self._file.closed:
                self._file.close()
