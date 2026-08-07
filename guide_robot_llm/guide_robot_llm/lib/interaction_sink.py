"""Построчный jsonl-лог ходов диалога, один файл на сессию (llm_plam.md §6).

Флаш после каждой записи, а не буферизация -- лог нужен именно для разбора
инцидентов (обрыв бэкенда, потерянная клауза), и падение процесса не должно
стоить последних записей. Стоимость `flush()` на каждый ход (не на каждый
токен) пренебрежимо мала.

Восстановлено почти дословно из `lib/turn_log.py` дорефакторинговой версии
пакета (`git show <pre-refactor>:guide_robot_llm/guide_robot_llm/lib/turn_log.py`) --
код был уже правильным, менять было нечего кроме имени класса и файлового
префикса (`chat_` -> `interaction_`, схема записи теперь другая -- ReAct
tool-calling, не прямой чат, см. `dialog/interaction_log.py`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TextIO

__all__ = ["InteractionSink"]


class InteractionSink:
    """Пишет по одной jsonl-строке на ход в `log_dir/interaction_YYYYmmdd_HHMMSS.jsonl`."""

    def __init__(self, log_dir: str | Path, session_start: float | None = None) -> None:
        """Открыть файл лога сессии. Каталог создаётся при отсутствии."""
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        started_at = session_start if session_start is not None else time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started_at))
        self._path = directory / f"interaction_{stamp}.jsonl"
        self._file: TextIO = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        """Путь к файлу текущей сессии."""
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        """Записать одну строку хода и сразу сбросить буфер на диск."""
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        """Закрыть файл. Безопасно вызывать повторно."""
        if not self._file.closed:
            self._file.close()
