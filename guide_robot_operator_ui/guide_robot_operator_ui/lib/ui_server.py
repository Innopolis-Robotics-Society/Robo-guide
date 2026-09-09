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

Аутентификация (design E2) -- тот же принцип: `UiServer` не хранит сессий и
не знает бэкендов, только маршрутизирует четыре `/api/auth/*` POST/GET'а на
коллбэки и гейтит `GATED_COMMAND_PATHS` через middleware по списку путей
(не декоратор на каждом хэндлере -- иначе новый командный роут окажется
незащищённым по забывчимости, см. test_ui_server.py's route-list guard).
Гейт срабатывает раньше state-проверок внутри хэндлеров: залоченный
оператор получает 401, не 409 (409 в design C2 закреплён за отказом по
состоянию робота).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from aiohttp import WSMsgType, web

__all__ = ["GATED_COMMAND_PATHS", "UiServer"]

# Командные пути Task C, требующие валидную сессию оператора (design E2).
# Тест-«сторож» (test_ui_server.py) сверяет это множество с фактическим
# списком POST-роутов под /api/ -- новый незакрытый командный роут роняет
# тест, а не остаётся незащищённым по забывчивости.
GATED_COMMAND_PATHS = frozenset(
    {"/api/tour/start", "/api/tour/stop", "/api/go_home", "/api/localization/reset"}
)


def _bearer_token(header: str) -> str | None:
    """Достать токен из `Authorization: Bearer <token>`; None, если формат не тот."""
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    return token or None


class _CommandCallback(Protocol):
    async def __call__(self, **kwargs: object) -> tuple[int, dict[str, Any]]: ...


class _AuthCheckCallback(Protocol):
    async def __call__(self, token: str | None) -> tuple[bool, str]: ...


class _AuthTouchCallback(Protocol):
    async def __call__(self, token: str) -> None: ...


class _CommandLoggedCallback(Protocol):
    async def __call__(self, *, path: str, operator: str, status: int) -> None: ...


class UiServer:
    """Отдаёт web/, /media/*, держит WS с последним кадром, проксирует команды."""

    def __init__(
        self,
        *,
        web_root: Path,
        media_root: Path,
        promo_root: Path,
        on_tours: _CommandCallback,
        on_tour_start: _CommandCallback,
        on_tour_stop: _CommandCallback,
        on_go_home: _CommandCallback,
        on_localization_reset: _CommandCallback,
        on_media: _CommandCallback,
        on_promo: _CommandCallback,
        on_auth_challenge: _CommandCallback,
        on_auth_verify: _CommandCallback,
        on_auth_logout: _CommandCallback,
        on_auth_status: _CommandCallback,
        on_auth_check: _AuthCheckCallback,
        on_auth_touch: _AuthTouchCallback,
        on_command_logged: _CommandLoggedCallback,
    ) -> None:
        """Собрать aiohttp.Application; ни один аргумент не завязан на rclpy."""
        self._web_root = Path(web_root)
        self._media_root = Path(media_root)
        self._promo_root = Path(promo_root)
        self._on_tours = on_tours
        self._on_tour_start = on_tour_start
        self._on_tour_stop = on_tour_stop
        self._on_go_home = on_go_home
        self._on_localization_reset = on_localization_reset
        self._on_media = on_media
        self._on_promo = on_promo
        self._on_auth_challenge = on_auth_challenge
        self._on_auth_verify = on_auth_verify
        self._on_auth_logout = on_auth_logout
        self._on_auth_status = on_auth_status
        self._on_auth_check = on_auth_check
        self._on_auth_touch = on_auth_touch
        self._on_command_logged = on_command_logged

        self._clients: set[web.WebSocketResponse] = set()
        self._last_frame: dict[str, Any] | None = None

        self.app = web.Application(middlewares=[self._auth_gate])
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws", self._handle_ws)
        self.app.router.add_get("/api/tours", self._handle_tours)
        self.app.router.add_post("/api/tour/start", self._handle_tour_start)
        self.app.router.add_post("/api/tour/stop", self._handle_tour_stop)
        self.app.router.add_post("/api/go_home", self._handle_go_home)
        self.app.router.add_post("/api/localization/reset", self._handle_localization_reset)
        self.app.router.add_get("/api/media/{exhibit_id}", self._handle_media)
        self.app.router.add_get("/api/promo", self._handle_promo)
        self.app.router.add_post("/api/auth/challenge", self._handle_auth_challenge)
        self.app.router.add_post("/api/auth/verify", self._handle_auth_verify)
        self.app.router.add_post("/api/auth/logout", self._handle_auth_logout)
        self.app.router.add_get("/api/auth/status", self._handle_auth_status)
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
        # Тот же паттерн для promo/media (design F2) -- promo_root
        # опционален по своей природе (промо не курируется, F1), а не
        # только временно недоступен, как media_root до слияния Task B.
        if self._promo_root.is_dir():
            self.app.router.add_static("/promo", self._promo_root, show_index=False)
        else:
            self.app.router.add_get("/promo/{tail:.*}", self._handle_promo_missing)
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

    # -- гейт (design E2) ---------------------------------------------------

    @web.middleware
    async def _auth_gate(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Требовать валидную сессию на GATED_COMMAND_PATHS, раньше state-гейта хэндлера.

        Продлевает окно сессии (`on_auth_touch`) только на исход < 400 --
        отклонённая по состоянию робота команда (409) не продлевает сессию
        так же, как и не прошедшая аутентификацию (401/403/429).
        """
        if request.path not in GATED_COMMAND_PATHS:
            return await handler(request)

        token = _bearer_token(request.headers.get("Authorization", ""))
        ok, operator = (False, "") if token is None else await self._on_auth_check(token)
        if not ok:
            await self._on_command_logged(path=request.path, operator="", status=401)
            return web.json_response({"error": "auth_required"}, status=401)

        response = await handler(request)
        await self._on_command_logged(path=request.path, operator=operator, status=response.status)
        if response.status < 400:
            assert token is not None  # ok=True только когда token не None
            await self._on_auth_touch(token)
        return response

    # -- статика / WS ------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        del request
        return web.FileResponse(self._web_root / "index.html")

    async def _handle_media_missing(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=404, text="media_root not configured (see operator_ui log)")

    async def _handle_promo_missing(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=404, text="promo_root not configured (see operator_ui log)")

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

    # -- аутентификация (design E2) -- ungated, см. GATED_COMMAND_PATHS ------

    async def _handle_auth_challenge(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_auth_challenge()
        return web.json_response(body, status=status)

    async def _handle_auth_verify(self, request: web.Request) -> web.Response:
        data = await self._read_json_object(request)
        if data is None:
            return web.json_response({"message": "invalid_body"}, status=400)
        status, body = await self._on_auth_verify(**data)
        return web.json_response(body, status=status)

    async def _handle_auth_logout(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_auth_logout()
        return web.json_response(body, status=status)

    async def _handle_auth_status(self, request: web.Request) -> web.Response:
        del request
        status, body = await self._on_auth_status()
        return web.json_response(body, status=status)

    async def _handle_media(self, request: web.Request) -> web.Response:
        """Манифест слайдов экспоната (design D2) -- {title, chunk_ids, items}."""
        exhibit_id = request.match_info["exhibit_id"]
        status, body = await self._on_media(exhibit_id=exhibit_id)
        return web.json_response(body, status=status)

    async def _handle_promo(self, request: web.Request) -> web.Response:
        """Манифест промо-петли (design F2) -- {items, promo_interval_s}. Без гейта."""
        del request
        status, body = await self._on_promo()
        return web.json_response(body, status=status)
