"""Типы ошибок llm_client (llm_plam.md §4, шаг 4).

Отдельный модуль, не вложенные классы в `backend.py` -- `ladder.py` и
будущий `dialog_agent` (шаг 5) обязаны различать `BackendAborted` (ретраить
нельзя и не нужно -- barge-in уже решил исход хода) от остальных (ретраить
можно) без импорта `backend.py` целиком.
"""

from __future__ import annotations

__all__ = ["BackendAborted", "BackendError", "BackendHTTPError", "BackendTimeout"]


class BackendError(Exception):
    """Базовая ошибка бэкенда -- уже пригодна для лога/ответа наверх."""


class BackendHTTPError(BackendError):
    """Сервер ответил не 2xx."""

    def __init__(self, status_code: int, body: str = "") -> None:
        """Запомнить статус и (обрезанное) тело ответа для диагностики."""
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class BackendTimeout(BackendError):
    """connect_timeout_s или read_timeout_s истёк."""


class BackendAborted(BackendError):
    """`abort_event` взведён в процессе стрима (barge-in) -- ход прерван намеренно."""
