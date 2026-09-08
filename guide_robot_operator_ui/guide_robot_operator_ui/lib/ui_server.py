"""aiohttp-сервер панели оператора: статика + WS push + командные POST'ы.

Никаких импортов rclpy -- структурная копия
guide_robot_face/guide_robot_face/face_server.py (web/статика/WS-реплей
последнего кадра), но, в отличие от него, WS здесь односторонний
("сервер -> клиент", команды идут отдельными POST'ами -- design C2,
"не смешивать"). Команды -- это единственная часть сервера, которой
приходится дожидаться результата ROS-вызова: конструктор принимает
асинхронные коллбэки (`on_tours`/`on_tour_start`/...), каждый из которых
возвращает `(http_status, body_dict)`; сервер только сериализует и не
знает, что стоит за коллбеком -- ROS-мост живёт в operator_ui_node.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from aiohttp import WSMsgType, web

__all__ = ["UiServer"]


class _CommandCallback(Protocol):
    async def __call__(self, **kwargs: object) -> tuple[int, dict[str, Any]]: ...


class UiServer:
    """Отдаёт web/, /media/*, держит WS с последним кадром, проксирует команды."""

    def __init__(
        self,
        *,
        web_root: Path,
        media_root: Path,
        on_tours: _CommandCallback,
        on_tour_start: _CommandCallback,
        on_tour_stop: _CommandCallback,
        on_go_home: _CommandCallback,
        on_localization_reset: _CommandCallback,
        on_media: _CommandCallback,
    ) -> None:
        """Собрать aiohttp.Application; ни один аргумент не завязан на rclpy."""
        self._web_root = Path(web_root)
        self._media_root = Path(media_root)
        self._on_tours = on_tours
        self._on_tour_start = on_tour_start
        self._on_tour_stop = on_tour_stop
        self._on_go_home = on_go_home
        self._on_localization_reset = on_localization_reset
        self._on_media = on_media

        self._clients: set[web.WebSocketResponse] = set()
        self._last_frame: dict[str, Any] | None = None

        self.app = web.Application()
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws", self._handle_ws)
        self.app.router.add_get("/api/tours", self._handle_tours)
        self.app.router.add_post("/api/tour/start", self._handle_tour_start)
        self.app.router.add_post("/api/tour/stop", self._handle_tour_stop)
        self.app.router.add_post("/api/go_home", self._handle_go_home)
        self.app.router.add_post("/api/localization/reset", self._handle_localization_reset)
        self.app.router.add_get("/api/media/{exhibit_id}", self._handle_media)
        # aiohttp.web.StaticResource требует существующую директорию УЖЕ ПРИ
        # РЕГИСТРАЦИИ (Path.resolve(strict=True) внутри add_static) -- падает
        # с ValueError на несуществующей media_root, проверено эмпирически
        # (test_ui_server.py). Значит "маршрут регистрируется, но не
        # используется" (design C7) при отсутствующей директории -- это
        # заглушка-хендлер, отвечающая 404, а не add_static().
        if self._media_root.is_dir():
            self.app.router.add_static("/media", self._media_root, show_index=False)
        else:
            self.app.router.add_get("/media/{tail:.*}", self._handle_media_missing)
        self.app.router.add_static("/static", self._web_root, show_index=False)

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

    async def push(self, frame: dict[str, Any]) -> None:
        """Новый кадр состояния -- запомнить (для реплея) и разослать клиентам."""
        self._last_frame = frame
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(frame)
            except ConnectionResetError:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # -- статика / WS ------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        del request
        return web.FileResponse(self._web_root / "index.html")

    async def _handle_media_missing(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=404, text="media_root not configured (see operator_ui log)")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            # Как и face_server.py -- сразу отдать последний кадр новому
            # клиенту, не дожидаясь следующего обновления /mission/state
            # (design C4, TRANSIENT_LOCAL-подобное восстановление UI).
            if self._last_frame is not None:
                await ws.send_json(self._last_frame)
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
        return ws

    # -- команды -------------------------------------------------------------

    @staticmethod
    async def _read_json_object(request: web.Request) -> dict[str, Any] | None:
        """Разобрать тело как JSON-объект; None -- невалидно (пустое тело допустимо)."""
        raw = await request.read()
        if not raw:
            return {}
        try:
            data = await request.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def _handle_tours(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_tours()
        return web.json_response(body, status=status)

    async def _handle_tour_start(self, request: web.Request) -> web.Response:
        data = await self._read_json_object(request)
        tour_id = data.get("tour_id") if data is not None else None
        if not isinstance(tour_id, str) or not tour_id:
            return web.json_response({"message": "invalid_body"}, status=400)
        status, body = await self._on_tour_start(tour_id=tour_id)
        return web.json_response(body, status=status)

    async def _handle_tour_stop(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_tour_stop()
        return web.json_response(body, status=status)

    async def _handle_go_home(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_go_home()
        return web.json_response(body, status=status)

    async def _handle_localization_reset(self, request: web.Request) -> web.Response:
        data = await self._read_json_object(request)
        if data is None or data.get("confirm") is not True:
            return web.json_response({"message": "confirm_required"}, status=400)
        status, body = await self._on_localization_reset()
        return web.json_response(body, status=status)

    async def _handle_media(self, request: web.Request) -> web.Response:
        """Манифест слайдов экспоната (design D2) -- {title, chunk_ids, items}."""
        exhibit_id = request.match_info["exhibit_id"]
        status, body = await self._on_media(exhibit_id=exhibit_id)
        return web.json_response(body, status=status)
