"""aiohttp-сервер лица: статика + websocket. Никаких импортов rclpy.

Управляется снаружи через ``push()`` (простой callback -- см.
CLAUDE_CODE_TASK_face_stage1.md §1.4), поэтому тестируется без ROS-графа
через aiohttp.test_utils.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web


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
