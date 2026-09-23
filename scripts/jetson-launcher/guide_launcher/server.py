"""HTTP-приложение launcher: страница, конфиг, промо, статус стека, прокси /ros/*."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from .api import LOCKOUT_S, MAX_FAILED_ATTEMPTS, NONCE_TTL_S, Api
from .auth import AuthChain, RfidBackend, make_auth_chain
from .bridge import Bridge, FrameWatcher
from .config import Config
from .journal import Journal
from .promo import PromoStore
from .rfid_link import PySerialPort, ReconnectingRfidLink, SerialPort
from .session import SessionManager
from .stack import StackMonitor, StackState
from .state import TourState

log = logging.getLogger(__name__)

# Ключи приложения строками: python3-aiohttp 3.8 (Ubuntu 22.04) не знает web.AppKey.
CFG = "cfg"
BRIDGE = "bridge"
MONITOR = "monitor"
WATCHER = "watcher"
PROMO = "promo"
TASKS = "tasks"
JOURNAL = "journal"

DEFAULT_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Cache-Control"] = "no-cache"


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(request.app["web_dir"] / "index.html")


async def _config(request: web.Request) -> web.Response:
    cfg: Config = request.app[CFG]
    return web.json_response(
        {
            "always_promo": cfg.always_promo,
            "slide_interval_s": cfg.slide_interval_s,
            "promo_interval_s": cfg.promo_interval_s,
            "stack_control": cfg.stack_control,
        }
    )


async def _promo_manifest(request: web.Request) -> web.Response:
    return web.json_response(request.app[PROMO].manifest())


async def _promo_file(request: web.Request) -> web.StreamResponse:
    root = request.app[PROMO].media_root.resolve()
    target = (root / request.match_info["tail"]).resolve()
    if root not in target.parents or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


def _read_secret(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("rfid_secret_file %s: не прочитан (%s), RFID недоступен", path, exc)
        return ""


def build_auth_chain(cfg: Config) -> AuthChain:
    """Цепочка входа из конфига; RFID без порта/секрета остаётся недоступным, не падает."""
    rfid: RfidBackend | None = None
    if "rfid" in cfg.auth_backends:

        def open_port() -> SerialPort:
            try:
                return PySerialPort(cfg.rfid_port)
            except ImportError as exc:
                raise OSError(f"pyserial не установлен: {exc}") from exc

        secret = _read_secret(cfg.rfid_secret_file)
        if not secret:
            log.warning("RFID: секрет не задан (rfid_secret_file), вход по карте недоступен")
        rfid = RfidBackend(ReconnectingRfidLink(open_port), secret)
    return make_auth_chain(
        list(cfg.auth_backends), operator_pin=cfg.operator_pin, rfid_backend=rfid
    )


async def _stack_status(request: web.Request) -> web.Response:
    return web.json_response(request.app[MONITOR].status())


def create_app(
    cfg: Config,
    *,
    monitor: StackMonitor | None = None,
    bridge: Bridge | None = None,
    watcher: FrameWatcher | None = None,
    promo: PromoStore | None = None,
    run_background: bool = True,
    auth: AuthChain | None = None,
    sessions: SessionManager | None = None,
    journal: Journal | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> web.Application:
    """Собрать приложение; зависимости можно подменить (тесты), фон -- отключить."""
    app = web.Application()
    app["web_dir"] = Path(cfg.web_dir) if cfg.web_dir else DEFAULT_WEB_DIR
    bridge = bridge or Bridge(cfg.bridge_url, cfg.bridge_token())
    monitor = monitor or StackMonitor(cfg, bridge.probe)
    watcher = watcher or FrameWatcher(bridge, lambda: monitor.state is StackState.UP)
    promo = promo or PromoStore(Path(cfg.promo_dir), cfg.promo_interval_s)
    app[CFG], app[BRIDGE], app[MONITOR], app[WATCHER], app[PROMO] = (
        cfg,
        bridge,
        monitor,
        watcher,
        promo,
    )
    app[TASKS] = []

    journal = journal or Journal(cfg.state_dir)
    sessions = sessions or SessionManager(
        session_ttl_s=cfg.session_ttl_s,
        nonce_ttl_s=NONCE_TTL_S,
        max_failed_attempts=MAX_FAILED_ATTEMPTS,
        lockout_s=LOCKOUT_S,
        time_fn=clock,
    )
    api = Api(
        cfg,
        monitor=monitor,
        bridge=bridge,
        watcher=watcher,
        sessions=sessions,
        auth=auth or build_auth_chain(cfg),
        journal=journal,
        tour=TourState(cfg.state_dir, cfg.default_tour),
        clock=clock,
    )
    if monitor.restart_guard is None:
        monitor.restart_guard = api.restart_guard
    app[JOURNAL] = journal

    app.on_response_prepare.append(_no_cache)
    app.router.add_get("/", _index)
    app.router.add_static("/static", app["web_dir"])
    app.router.add_get("/api/config", _config)
    app.router.add_get("/api/promo", _promo_manifest)
    app.router.add_get("/promo/{tail:.+}", _promo_file)
    app.router.add_get("/api/stack/status", _stack_status)
    api.register(app)
    app.router.add_route("*", "/ros/{tail:.*}", bridge.proxy)

    if run_background:
        app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)
    return app


async def _start_background(app: web.Application) -> None:
    for coro in (app[MONITOR].run_forever(), app[WATCHER].run_forever()):
        app[TASKS].append(asyncio.ensure_future(coro))


async def _stop_background(app: web.Application) -> None:
    """Остановить только фоновые задачи launcher; ROS-стек не трогаем."""
    for task in app[TASKS]:
        task.cancel()
    for task in app[TASKS]:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await app[BRIDGE].close()
    app[JOURNAL].close()
