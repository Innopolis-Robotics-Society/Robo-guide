"""Сессия оператора: nonce, токен, локаут перебора -- без rclpy/aiohttp.

Разделение с lib/auth.py: auth.py решает "кто ты" (бэкенды), session.py --
"как долго тебе верить" (nonce/TTL/локаут). Часы инжектируются
(`time_fn`/`wall_time_fn`), чтобы скользящий TTL и локаут перебора
тестировались без sleep. Внутренний отсчёт -- монотонные часы: скачок
системного времени назад/вперёд не должен продлевать или обрывать сессию;
`wall_time_fn` пересчитывается в стенные секунды только на выходе, для
`expires_at` в HTTP-ответе.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["EvictedSession", "SessionInfo", "SessionManager"]


@dataclass(frozen=True)
class SessionInfo:
    """Активная сессия. `expires_at_wall` -- готовое поле для HTTP-ответа."""

    token: str
    operator: str
    backend: str
    expires_at_wall: float


@dataclass(frozen=True)
class EvictedSession:
    """Сессия, вытесненная новым успешным входом -- для строки в jsonl-лог."""

    operator: str
    backend: str


@dataclass
class _Nonce:
    expires_at: float


@dataclass
class _Session:
    token: str
    operator: str
    backend: str
    expires_at: float


class SessionManager:
    """Одна активная сессия на узел + одноразовые nonce + локаут перебора."""

    def __init__(
        self,
        *,
        session_ttl_s: float,
        nonce_ttl_s: float,
        max_failed_attempts: int,
        lockout_s: float,
        time_fn: Callable[[], float] = time.monotonic,
        wall_time_fn: Callable[[], float] = time.time,
    ) -> None:
        """Настроить TTL/локаут; часы инжектируются -- см. докстринг модуля."""
        self._session_ttl_s = session_ttl_s
        self._nonce_ttl_s = nonce_ttl_s
        self._max_failed_attempts = max_failed_attempts
        self._lockout_s = lockout_s
        self._now = time_fn
        self._wall_now = wall_time_fn

        self._nonces: dict[str, _Nonce] = {}
        self._session: _Session | None = None
        self._failed_attempts = 0
        self._locked_until: float | None = None

    # -- nonce (design E2: одноразовый, "backends" список -- вопрос auth.py) --

    def issue_nonce(self) -> str:
        """Выдать новый одноразовый nonce; заодно подмести протухшие."""
        now = self._now()
        expired = [value for value, nonce in self._nonces.items() if nonce.expires_at <= now]
        for value in expired:
            del self._nonces[value]
        value = secrets.token_hex(32)
        self._nonces[value] = _Nonce(expires_at=now + self._nonce_ttl_s)
        return value

    def consume_nonce(self, nonce: str) -> bool:
        """Погасить nonce НЕЗАВИСИМО от исхода проверки; True -- он был валиден.

        Вызывается ровно один раз на попытку /api/auth/verify, до разбора
        backend/локаута -- повтор того же nonce после любого исхода (успех,
        отказ, локаут) обязан провалиться (design E2, критерий 5).
        """
        entry = self._nonces.pop(nonce, None)
        if entry is None:
            return False
        return entry.expires_at > self._now()

    # -- локаут перебора --------------------------------------------------------

    def lockout_remaining_s(self) -> float | None:
        """Секунды до конца блокировки перебора; None -- не заблокировано."""
        if self._locked_until is None:
            return None
        remaining = self._locked_until - self._now()
        if remaining <= 0:
            self._locked_until = None
            self._failed_attempts = 0
            return None
        return remaining

    def record_failure(self) -> None:
        """Учесть неудачную попытку; при достижении порога -- заблокировать."""
        self._failed_attempts += 1
        if self._failed_attempts >= self._max_failed_attempts:
            self._locked_until = self._now() + self._lockout_s

    # -- сессия ------------------------------------------------------------------

    def create_session(
        self, *, operator: str, backend: str
    ) -> tuple[SessionInfo, EvictedSession | None]:
        """Открыть сессию, сбросить счётчик неудач; вытеснить прежнюю, если была.

        Одна активная сессия на узел (design E2) -- второй успешный вход
        вытесняет первый; вызывающий отвечает за строку в лог, вытеснение
        только возвращается, не логируется здесь (модуль без ввода-вывода).
        """
        self._failed_attempts = 0
        self._locked_until = None
        evicted = None
        if self._session is not None:
            evicted = EvictedSession(
                operator=self._session.operator, backend=self._session.backend
            )
        token = secrets.token_urlsafe(32)
        self._session = _Session(
            token=token,
            operator=operator,
            backend=backend,
            expires_at=self._now() + self._session_ttl_s,
        )
        return self._to_info(self._session), evicted

    def validate(self, token: str | None) -> SessionInfo | None:
        """Проверить токен БЕЗ продления окна (используется гейтом на входе)."""
        session = self._active_session()
        if session is None or token is None or session.token != token:
            return None
        return self._to_info(session)

    def touch(self, token: str) -> None:
        """Продлить скользящее окно, если токен -- всё ещё активная сессия.

        Вызывается после того, как гейтованная команда УЖЕ выполнена
        успешно (design E2: "каждая успешная команда продлевает окно") --
        не на каждый запрос, а только на исход < 400.
        """
        session = self._active_session()
        if session is not None and session.token == token:
            session.expires_at = self._now() + self._session_ttl_s

    def logout(self) -> SessionInfo | None:
        """Закрыть активную сессию (если есть) и вернуть её -- для лога."""
        session = self._active_session()
        self._session = None
        return self._to_info(session) if session is not None else None

    def status(self) -> SessionInfo | None:
        """Текущая активная сессия (для GET /api/auth/status)."""
        session = self._active_session()
        return self._to_info(session) if session is not None else None

    def _active_session(self) -> _Session | None:
        if self._session is None:
            return None
        if self._session.expires_at <= self._now():
            self._session = None
            return None
        return self._session

    def _to_info(self, session: _Session) -> SessionInfo:
        remaining = max(0.0, session.expires_at - self._now())
        return SessionInfo(
            token=session.token,
            operator=session.operator,
            backend=session.backend,
            expires_at_wall=self._wall_now() + remaining,
        )
