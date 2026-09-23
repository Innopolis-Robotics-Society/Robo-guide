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
from .state import TourState, valid_tour_id

log = logging.getLogger(__name__)

COOKIE = "gl_session"
API = "api"

NONCE_TTL_S = 30.0
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_S = 60.0
PUBLIC_START_MIN_INTERVAL_S = 10.0
MISSION_STALE_S = 3.0
IDLE_STATES = frozenset({"idle", "unknown"})
FAULT_SUPERVISOR_STATES = frozenset({"FAULT", "SHUTDOWN"})

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def mission_link_ok(frame: Frame | None) -> bool:
    """Связь с mission_fsm есть: кадр получен и данные свежее MISSION_STALE_S."""
    if frame is None:
        return False
    age = frame.data.get("mission_state_age_s")
    return isinstance(age, (int, float)) and age <= MISSION_STALE_S


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
        tour: TourState,
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
        self.tour = tour
        self.clock = clock
        self._last_public_start: float | None = None
        self._tasks: set[asyncio.Future] = set()

    def restart_guard(self) -> str | None:
        """Причина отказа рестарта стека: тур идёт по последнему кадру."""
        return "robot_moving" if tour_active(self.watcher.latest()) else None

    def register(self, app: web.Application) -> None:
        """Добавить роуты в приложение и погасить фоновые задачи при остановке."""
        app[API] = self
        add = app.router.add_route
        add("POST", "/api/auth/challenge", self.auth_challenge)
        add("POST", "/api/auth/verify", self.auth_verify)
        add("POST", "/api/auth/logout", self.auth_logout)
        add("GET", "/api/auth/status", self.auth_status)
        add("GET", "/api/tour/current", self.tour_current)
        add("POST", "/api/tour/current", self._authed(self.tour_set))
        add("POST", "/api/stack/start", self._authed(self.stack_start))
        add("POST", "/api/stack/restart", self._authed(self.stack_restart))
        add("GET", "/api/stack/log", self._authed(self.stack_log))
        add("POST", "/api/op/tour/start", self._authed(self._op("/api/tour/start", tour=True)))
        add("POST", "/api/op/tour/stop", self._authed(self._op("/api/tour/stop")))
        add("POST", "/api/op/go_home", self._authed(self._op("/api/go_home")))
        reset_body = {"confirm": True}
        reset = self._op("/api/localization/reset", body=reset_body)
        add("POST", "/api/op/localization/reset", self._authed(reset))
        add("POST", "/api/op/costmaps/clear", self._authed(self._op("/api/costmaps/clear")))
        add("POST", "/api/public/start_tour", self.public_start_tour)

    # -- сессия -----------------------------------------------------------------

    def _authed(self, handler: Handler) -> Handler:
        async def wrapper(request: web.Request) -> web.StreamResponse:
            token = request.cookies.get(COOKIE)
            info = self.sessions.validate(token)
            if info is None:
                return web.json_response({"error": "auth_required"}, status=401)
            request["operator"] = info.operator
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

    # -- текущий тур ------------------------------------------------------------

    async def tour_current(self, request: web.Request) -> web.Response:
        """Текущий тур: из state.json или default_tour."""
        tour_id, source = self.tour.current()
        return web.json_response({"tour_id": tour_id, "source": source})

    async def tour_set(self, request: web.Request) -> web.Response:
        """Сделать тур текущим (только с входом)."""
        data = await _json_object(request)
        tour_id = data.get("tour_id")
        operator = request["operator"]
        if not valid_tour_id(tour_id):
            self.journal.write(
                "tour_current", operator=operator, tour_id=str(tour_id)[:64], status=400
            )
            return web.json_response({"error": "invalid_body"}, status=400)
        previous, _ = self.tour.current()
        self.tour.set(tour_id)
        self.journal.write(
            "tour_current", operator=operator, tour_id=tour_id, previous=previous, status=200
        )
        return web.json_response({"tour_id": tour_id, "source": "state"})

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

    # -- команды оператора ------------------------------------------------------

    def _op(
        self, bridge_path: str, *, body: dict[str, Any] | None = None, tour: bool = False
    ) -> Handler:
        async def handler(request: web.Request) -> web.Response:
            operator = request["operator"]
            if self.monitor.state is not StackState.UP:
                self._log_command(bridge_path, operator, 409)
                return web.json_response({"error": "ros_down"}, status=409)
            payload = dict(body or {})
            if tour:
                payload["tour_id"] = self.tour.current()[0]
            status, upstream = await self.bridge.command(bridge_path, payload, operator)
            self._log_command(bridge_path, operator, status, tour_id=payload.get("tour_id"))
            return web.json_response(upstream, status=status)

        return handler

    def _log_command(self, path: str, operator: str, status: int, **extra: Any) -> None:
        extra = {k: v for k, v in extra.items() if v is not None}
        self.journal.write("command", path=path, operator=operator, status=status, **extra)

    # -- публичный старт --------------------------------------------------------

    async def public_start_tour(self, request: web.Request) -> web.Response:
        """Старт текущего тура без входа; tour_id и тело клиента игнорируются."""
        reason = await self._public_refusal()
        tour_id, _ = self.tour.current()
        if reason is not None:
            self.journal.write("public_start", tour_id=tour_id, ok=False, reason=reason)
            return web.json_response({"error": "refused", "reason": reason}, status=409)

        status, upstream = await self.bridge.command(
            "/api/tour/start", {"tour_id": tour_id}, "public"
        )
        if 200 <= status < 300:
            self.journal.write("public_start", tour_id=tour_id, ok=True, status=status)
            return web.json_response({"ok": True, "tour_id": tour_id})
        detail = ""
        if isinstance(upstream, dict):
            detail = upstream.get("error") or upstream.get("message") or ""
        reason = f"upstream_{status}"
        self.journal.write(
            "public_start", tour_id=tour_id, ok=False, reason=reason, detail=str(detail)
        )
        return web.json_response(
            {"error": "refused", "reason": reason}, status=409 if status < 500 else 502
        )

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
        status, body = await self.bridge.get_json("/api/tours")
        if status != 200 or not isinstance(body, dict):
            return "ros_down"
        known = {t.get("id") for t in body.get("tours", []) if isinstance(t, dict)}
        if self.tour.current()[0] not in known:
            return "unknown_tour"
        return None
