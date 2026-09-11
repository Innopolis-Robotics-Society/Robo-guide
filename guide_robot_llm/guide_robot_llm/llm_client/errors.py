"""Типы ошибок llm_client (llm_plam.md §4, шаг 4).

Отдельный модуль, не вложенные классы в `backend.py` -- `ladder.py` и
будущий `dialog_agent` (шаг 5) обязаны различать `BackendAborted` (ретраить
нельзя и не нужно -- barge-in уже решил исход хода) от остальных (ретраить
можно) без импорта `backend.py` целиком.

Taiga #3: конструкторы прогоняют текст ошибки через `redact_value` -- единая
точка, где строки ошибок гарантированно не несут Authorization-заголовков и
base64-payload'ов кадров (требование issue: "never appear in error strings").
"""

from __future__ import annotations

from guide_robot_llm.llm_client.redact import redact_value

__all__ = ["BackendAborted", "BackendError", "BackendHTTPError", "BackendTimeout"]


class BackendError(Exception):
    """Базовая ошибка бэкенда -- уже пригодна для лога/ответа наверх."""

    def __init__(self, message: str = "") -> None:
        """Сохранить текст ошибки после маскирования секретов (Taiga #3)."""
        super().__init__(redact_value(message))


class BackendHTTPError(BackendError):
    """Сервер ответил не 2xx."""

    def __init__(self, status_code: int, body: str = "") -> None:
        """Запомнить статус и (обрезанное) тело ответа для диагностики."""
        super().__init__(f"HTTP {status_code}: {redact_value(body[:200])}")
        self.status_code = status_code
        self.body = body


class BackendTimeout(BackendError):
    """connect_timeout_s или read_timeout_s истёк."""


class BackendAborted(BackendError):
    """`abort_event` взведён в процессе стрима (barge-in) -- ход прерван намеренно."""
