"""Независимый хостовый API с необязательным эталонным UI."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from .api import LOCKOUT_S, MAX_FAILED_ATTEMPTS, NONCE_TTL_S
from .auth import AuthChain, RfidBackend, make_auth_chain
from .bridge import Bridge, FrameWatcher
from .config import Config
from .http_policy import policy, prepare
from .journal import Journal
from .reference import register as register_reference
from .rfid_link import PySerialPort, ReconnectingRfidLink, SerialPort
from .session import SessionManager
from .stack import StackMonitor, StackState
from .v1 import RobotApi

log = logging.getLogger(__name__)

# Ключи приложения строками: python3-aiohttp 3.8 (Ubuntu 22.04) не знает web.AppKey.
CFG = "cfg"
BRIDGE = "bridge"
MONITOR = "monitor"
WATCHER = "watcher"
TASKS = "tasks"
JOURNAL = "journal"


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


def create_app(
    cfg: Config,
    *,
    monitor: StackMonitor | None = None,
    bridge: Bridge | None = None,
    watcher: FrameWatcher | None = None,
    run_background: bool = True,
    auth: AuthChain | None = None,
    sessions: SessionManager | None = None,
    journal: Journal | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> web.Application:
    """Собрать приложение; зависимости можно подменить (тесты), фон -- отключить."""
    app = web.Application(middlewares=[policy])
    app.on_response_prepare.append(prepare)
    bridge = bridge or Bridge(cfg.bridge_url, cfg.bridge_token())
    monitor = monitor or StackMonitor(cfg, bridge.probe)
    watcher = watcher or FrameWatcher(bridge, lambda: monitor.state is StackState.UP)
    # Reference UI creates its own optional media store.
    app[CFG], app[BRIDGE], app[MONITOR], app[WATCHER] = (
        cfg,
        bridge,
        monitor,
        watcher,
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
    api = RobotApi(
        cfg,
        monitor=monitor,
        bridge=bridge,
        watcher=watcher,
        sessions=sessions,
        auth=auth or build_auth_chain(cfg),
        journal=journal,
        clock=clock,
    )
    if monitor.restart_guard is None:
        monitor.restart_guard = api.restart_guard
    app[JOURNAL] = journal

    api.register(app)
    from .system_settings import SystemSettings

    app["system_settings"] = SystemSettings(api)
    app["system_settings"].register(app)
    from .power import PowerControl

    app["power"] = PowerControl(api)
    monitor.power_pending = lambda: app["power"].pending
    app["power"].register(app)
    register_reference(app, cfg, api)
    docs = Path(__file__).resolve().parent / "spec"
    if docs.is_dir():
        app.router.add_static("/api/v1/docs", docs)

    if run_background:
        app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)
    return app


async def _start_background(app: web.Application) -> None:
    for coro in (app[MONITOR].run_forever(), app[WATCHER].run_forever(), app["api"].events.run()):
        app[TASKS].append(asyncio.ensure_future(coro))


async def _stop_background(app: web.Application) -> None:
    """Остановить только фоновые задачи launcher; ROS-стек не трогаем."""
    for task in app[TASKS]:
        task.cancel()
    await asyncio.gather(*app[TASKS], return_exceptions=True)
    for task in list(app["api"]._tasks):
        task.cancel()
    await asyncio.gather(*app["api"]._tasks, return_exceptions=True)
    await app["api"].events.close()
    await app[BRIDGE].close()
    app[JOURNAL].close()
