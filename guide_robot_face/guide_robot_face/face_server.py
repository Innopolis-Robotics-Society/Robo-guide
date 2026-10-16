"""aiohttp-сервер лица: статика + websocket. Никаких импортов rclpy.

Управляется снаружи через ``push()`` (простой callback -- см.
CLAUDE_CODE_TASK_face_stage1.md §1.4), поэтому тестируется без ROS-графа
через aiohttp.test_utils.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

THEME_COLOR_KEYS = ("bg_color", "eye_color", "halo_color")
_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def apply_theme(
    states: dict[str, Any], theme: str, colors: dict[str, str] | None = None
) -> dict[str, Any]:
    """Выбрать тему лица (``global.themes``) и записать её в ``global.theme``.

    Пустая ``theme`` -- оставить ту, что в YAML. ``colors`` -- цвета вручную
    (ключи из ``THEME_COLOR_KEYS``, пустое значение -- цвет темы), ложатся
    поверх выбранной темы. Неизвестная тема, тема без одного из трёх цветов
    или не-hex цвет -- ValueError: лицо с непредсказуемыми цветами хуже, чем
    упавшая на старте нода. face.js берёт цвета из
    ``global.themes[global.theme]``.
    """
    g = states.setdefault("global", {})
    themes = g.get("themes") or {}
    name = theme or g.get("theme", "")
    overrides = {k: v for k, v in (colors or {}).items() if v}
    unknown_keys = set(overrides) - set(THEME_COLOR_KEYS)
    if unknown_keys:
        raise ValueError(f"неизвестные цвета лица: {', '.join(sorted(unknown_keys))}")
    if not name:
        if overrides:
            raise ValueError("цвета лица заданы, а темы нет (global.theme)")
        return states
    if name not in themes:
        known = ", ".join(sorted(themes)) or "нет"
        raise ValueError(f"неизвестная тема лица {name!r} (есть: {known})")
    resolved = {**themes[name], **overrides}
    for key in THEME_COLOR_KEYS:
        value = resolved.get(key)
        if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
            raise ValueError(f"тема лица {name!r}: {key}={value!r}, нужен #rgb/#rrggbb/#rrggbbaa")
    themes[name] = resolved
    g["theme"] = name
    return states


class FaceServer:
    """Отдаёт web/, /states.json и держит websocket с последним кадром."""

    def __init__(self, web_root: Path, states: dict[str, Any]) -> None:
        """Собрать aiohttp.Application; states -- уже разобранный face_states.yaml."""
        self._web_root = Path(web_root)
        self._states = states
        self._clients: set[web.WebSocketResponse] = set()
        self._last_frame: dict[str, Any] | None = None

        self.app = web.Application()
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/states.json", self._handle_states)
        self.app.router.add_get("/ws", self._handle_ws)
        self.app.router.add_static("/", self._web_root, show_index=False)

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self, host: str, port: int) -> None:
        """Поднять AppRunner + TCPSite; вернуться после того, как порт занят."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

    async def stop(self) -> None:
        """Закрыть все websocket-клиенты и остановить AppRunner."""
        for ws in list(self._clients):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def push(self, state: str, gaze_az: float, seq: int) -> None:
        """Новый кадр от face_node -- запомнить и разослать подключённым клиентам."""
        frame = {"state": state, "gaze_az": gaze_az, "seq": seq}
        self._last_frame = frame
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(frame)
            except ConnectionResetError:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        del request
        return web.FileResponse(self._web_root / "index.html")

    async def _handle_states(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(self._states)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            # Клиент ничего не шлёт (кроме дефолтного ping/pong) -- на
            # подключении сразу отдать последний известный кадр, чтобы
            # перезапуск браузера не оставлял лицо мёртвым (§3.3).
            if self._last_frame is not None:
                await ws.send_json(self._last_frame)
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
        return ws
