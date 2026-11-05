"""Авторизованные и публичные роуты launcher: вход, текущий тур, команды, стек."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .auth import AuthChain
from .bridge import Bridge, Frame, FrameWatcher
from .config import Config
from .journal import Journal
from .session import SessionManager
from .stack import StackError, StackMonitor, StackState

log = logging.getLogger(__name__)

COOKIE = "gl_session"
API = "api"

NONCE_TTL_S = 30.0
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_S = 60.0
PUBLIC_START_MIN_INTERVAL_S = 10.0
# Против двойного тапа: второе нажатие уже не застанет AWAITING_CONFIRM, но и не
# должно дойти до моста, пока первое в полёте.
PUBLIC_CONFIRM_MIN_INTERVAL_S = 1.5
PUBLIC_STOP_MIN_INTERVAL_S = 1.5
# Робот едет: тур между остановками или поездка на базу/стоянку/подзарядку.
MOVING_STATES = frozenset({"navigating", "returning"})
CONFIRM_ANSWERS = frozenset({"yes", "stop", "question"})
MISSION_STALE_S = 3.0
IDLE_STATES = frozenset({"idle"})
FAULT_SUPERVISOR_STATES = frozenset({"FAULT", "SHUTDOWN"})

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def mission_link_ok(frame: Frame | None) -> bool:
    """Связь с mission_fsm есть: кадр получен и данные свежее MISSION_STALE_S."""
    if frame is None:
        return False
    age = frame.data.get("mission_state_age_s")
    return (
        isinstance(age, int | float) and not isinstance(age, bool) and 0 <= age <= MISSION_STALE_S
    )


def tour_active(frame: Frame | None) -> bool:
    """Тур идёт: связь с mission_fsm жива и состояние не idle/unknown."""
    return (
        frame is not None
        and mission_link_ok(frame)
        and frame.data.get("state_name") not in IDLE_STATES
    )


def estop_active(frame: Frame | None) -> bool:
    """E-STOP: флаг в кадре или супервизор в FAULT/SHUTDOWN (как бейдж на странице)."""
    if frame is None:
        return False
    return bool(frame.data.get("estop")) or (
        frame.data.get("supervisor_state") in FAULT_SUPERVISOR_STATES
    )


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class Api:
    """Зависимости роутов и состояние (сессии, антифлуд публичного старта)."""

    def __init__(
        self,
        cfg: Config,
        *,
        monitor: StackMonitor,
        bridge: Bridge,
        watcher: FrameWatcher,
        sessions: SessionManager,
        auth: AuthChain,
        journal: Journal,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Собрать зависимости; `clock` -- monotonic, подменяется в тестах."""
        self.cfg = cfg
        self.monitor = monitor
        self.bridge = bridge
        self.watcher = watcher
        self.sessions = sessions
        self.auth = auth
        self.journal = journal
        self.clock = clock
        self._last_public_start: float | None = None
        self._last_public_confirm: float | None = None
        self._last_public_stop: float | None = None
        self._tasks: set[asyncio.Future] = set()

    def restart_guard(self) -> str | None:
        """Причина отказа рестарта стека: тур идёт по последнему кадру."""
        return "robot_moving" if tour_active(self.watcher.latest()) else None

    # -- сессия -----------------------------------------------------------------

    def _authed(self, handler: Handler) -> Handler:
        async def wrapper(request: web.Request) -> web.StreamResponse:
            token = request.cookies.get(COOKIE)
            info = self.sessions.validate(token)
            if info is None:
                return web.json_response({"error": "auth_required"}, status=401)
            request["operator"] = info.operator
            request["owner"] = info.token
            response = await handler(request)
            if request.method == "POST" and response.status < 400 and token:
                self.sessions.touch(token)
            return response

        return wrapper

    async def auth_challenge(self, request: web.Request) -> web.Response:
        """Выдать одноразовый nonce и список готовых способов входа."""
        return web.json_response(
            {"nonce": self.sessions.issue_nonce(), "backends": self.auth.available_names()}
        )

    async def auth_verify(self, request: web.Request) -> web.Response:
        """Проверить вход (nonce гасится при любом исходе) и выдать cookie сессии."""
        data = await _json_object(request)
        nonce, backend = data.get("nonce"), data.get("backend")
        if not isinstance(nonce, str) or not nonce or not isinstance(backend, str) or not backend:
            return web.json_response({"error": "invalid_body"}, status=400)
        if not self.sessions.consume_nonce(nonce):
            return web.json_response({"error": "invalid_nonce"}, status=401)

        remaining = self.sessions.lockout_remaining_s()
        if remaining is not None:
            self.journal.write(
                "auth_attempt", backend=backend, operator="", ok=False, reason="locked_out"
            )
            return web.json_response(
                {"error": "locked_out", "retry_after_s": remaining}, status=429
            )

        result = await asyncio.to_thread(self.auth.verify, nonce, backend, data)
        if not result.ok:
            # An empty reader during the bounded UI wait is not a bad credential.
            # Keep invalid signatures/PINs subject to the existing lockout.
            if result.reason == "rfid_no_card":
                return web.json_response(
                    {"error": "invalid_credentials", "reason": "rfid_no_card"}, status=401
                )
            self.sessions.record_failure()
            self.journal.write(
                "auth_attempt", backend=backend, operator="", ok=False, reason=result.reason
            )
            body: dict[str, Any] = {"error": "invalid_credentials"}
            if result.reason == "rfid_no_card":
                body["reason"] = "rfid_no_card"
            return web.json_response(body, status=401)

        info, evicted = self.sessions.create_session(operator=result.operator, backend=backend)
        if evicted is not None:
            self.journal.write(
                "session_evicted", operator=evicted.operator, backend=evicted.backend
            )
        self.journal.write("auth_attempt", backend=backend, operator=info.operator, ok=True)
        response = web.json_response(
            {"operator": info.operator, "expires_at": info.expires_at_wall}
        )
        response.set_cookie(COOKIE, info.token, httponly=True, samesite="Strict", path="/")
        return response

    async def auth_logout(self, request: web.Request) -> web.Response:
        """Закрыть сессию этого браузера (чужой cookie ничего не закрывает)."""
        info = self.sessions.validate(request.cookies.get(COOKIE))
        if info is not None:
            self.sessions.logout()
            self.journal.write("logout", operator=info.operator, backend=info.backend)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def auth_status(self, request: web.Request) -> web.Response:
        """Состояние сессии по cookie; окно не продлевается."""
        info = self.sessions.validate(request.cookies.get(COOKIE))
        if info is None:
            return web.json_response({"active": False, "expires_at": None, "operator": None})
        return web.json_response(
            {"active": True, "expires_at": info.expires_at_wall, "operator": info.operator}
        )

    # -- стек -------------------------------------------------------------------

    def _spawn(self, action: str, operator: str) -> asyncio.Future:
        """Запустить start/restart в фоне (обрыв соединения не должен прервать рестарт)."""
        coro = self.monitor.start() if action == "start" else self.monitor.restart()
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._stack_done(t, action, operator))
        return task

    def _stack_done(self, task: asyncio.Future, action: str, operator: str) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            self.journal.write("stack", action=action, operator=operator, phase="done", ok=True)
        elif isinstance(exc, StackError) and exc.status in (400, 409):
            return  # отказ уже записан фазой request
        elif isinstance(exc, StackError):
            self.journal.write(
                "stack",
                action=action,
                operator=operator,
                phase="done",
                ok=False,
                error=exc.reason,
                detail=self.monitor.last_error,
            )
        else:
            log.error("stack %s crashed", action, exc_info=exc)
            self.journal.write(
                "stack", action=action, operator=operator, phase="done", ok=False, error=repr(exc)
            )

    async def _stack_command(self, request: web.Request, action: str) -> web.Response:
        operator = request["operator"]
        task = self._spawn(action, operator)
        await asyncio.sleep(0)  # первый шаг корутины: там отказ (409/400) до долгих команд
        exc = task.exception() if task.done() and not task.cancelled() else None
        if isinstance(exc, StackError):
            self.journal.write(
                "stack", action=action, operator=operator, phase="request", status=exc.status
            )
            return web.json_response({"error": exc.reason}, status=exc.status)
        self.journal.write("stack", action=action, operator=operator, phase="request", status=202)
        return web.json_response({"ok": True, "state": self.monitor.state.value}, status=202)

    async def stack_start(self, request: web.Request) -> web.Response:
        """Запустить стек: 202, дальше состояние смотрят по /api/stack/status."""
        return await self._stack_command(request, "start")

    async def stack_restart(self, request: web.Request) -> web.Response:
        """Перезапустить стек: 409 robot_moving, если по последнему кадру идёт тур."""
        return await self._stack_command(request, "restart")

    async def stack_log(self, request: web.Request) -> web.Response:
        """Последние 30 строк stack_log из контейнера."""
        text = await self.monitor.log_tail(30)
        return web.json_response({"lines": text.splitlines()})

    # -- публичный ответ на «Идём дальше?» ---------------------------------------

    async def public_confirm(self, request: web.Request) -> web.Response:
        """Кнопки «Да» / «Стоп» / «Задать вопрос» без входа, только в AWAITING_CONFIRM."""
        answer = (await _json_object(request)).get("answer")
        if answer not in CONFIRM_ANSWERS:
            return web.json_response({"error": "invalid_answer"}, status=400)
        reason = self._public_confirm_refusal()
        if reason is not None:
            self.journal.write("public_confirm", answer=answer, ok=False, reason=reason)
            return web.json_response({"error": "refused", "reason": reason}, status=409)

        status, upstream = await self.bridge.command(
            "/internal/interaction/confirm", {"answer": answer}, "public"
        )
        if 200 <= status < 300:
            self.journal.write("public_confirm", answer=answer, ok=True, status=status)
            listening = isinstance(upstream, dict) and upstream.get("listening") is True
            return web.json_response({"ok": True, "answer": answer, "listening": listening})
        detail = ""
        if isinstance(upstream, dict):
            detail = upstream.get("error") or upstream.get("message") or ""
        reason = f"upstream_{status}"
        self.journal.write(
            "public_confirm", answer=answer, ok=False, reason=reason, detail=str(detail)
        )
        return web.json_response(
            {"error": "refused", "reason": reason}, status=409 if status < 500 else status
        )

    # -- публичный «Стоп» во время движения ---------------------------------------

    async def public_stop(self, request: web.Request) -> web.Response:
        """Остановить робота на месте без входа -- только пока он едет."""
        del request
        reason = self._public_stop_refusal()
        if reason is not None:
            self.journal.write("public_stop", ok=False, reason=reason)
            return web.json_response({"error": "refused", "reason": reason}, status=409)

        status, upstream = await self.bridge.command("/internal/missions/stop", {}, "public")
        if 200 <= status < 300:
            self.journal.write("public_stop", ok=True, status=status)
            return web.json_response({"ok": True})
        detail = ""
        if isinstance(upstream, dict):
            detail = upstream.get("error") or upstream.get("message") or ""
        reason = f"upstream_{status}"
        self.journal.write("public_stop", ok=False, reason=reason, detail=str(detail))
        return web.json_response(
            {"error": "refused", "reason": reason}, status=409 if status < 500 else status
        )

    def _public_stop_refusal(self) -> str | None:
        # E-STOP не проверяется: остановка безопасна в любом состоянии.
        now = self.clock()
        last = self._last_public_stop
        if last is not None and now - last < PUBLIC_STOP_MIN_INTERVAL_S:
            return "rate_limited"
        self._last_public_stop = now
        if self.monitor.state is not StackState.UP:
            return "ros_down"
        frame = self.watcher.latest()
        if not mission_link_ok(frame):
            return "no_mission_fsm"
        if frame is None or not (
            frame.data.get("moving") or frame.data.get("state_name") in MOVING_STATES
        ):
            return "not_moving"
        return None

    def _public_confirm_refusal(self) -> str | None:
        if not self.cfg.visitor_confirm_buttons:
            return "disabled"
        now = self.clock()
        last = self._last_public_confirm
        if last is not None and now - last < PUBLIC_CONFIRM_MIN_INTERVAL_S:
            return "rate_limited"
        self._last_public_confirm = now
        if self.monitor.state is not StackState.UP:
            return "ros_down"
        frame = self.watcher.latest()
        if not mission_link_ok(frame):
            return "no_mission_fsm"
        if estop_active(frame):
            return "estop"
        if frame is None or frame.data.get("state_name") != "awaiting_confirm":
            return "not_awaiting_confirm"
        return None

    async def _public_refusal(self) -> str | None:
        now = self.clock()
        last = self._last_public_start
        if last is not None and now - last < PUBLIC_START_MIN_INTERVAL_S:
            return "rate_limited"
        self._last_public_start = now
        if self.monitor.state is not StackState.UP:
            return "ros_down"
        frame = self.watcher.latest()
        if not mission_link_ok(frame):
            return "no_mission_fsm"
        if estop_active(frame):
            return "estop"
        if tour_active(frame):
            return "tour_active"
        return None
