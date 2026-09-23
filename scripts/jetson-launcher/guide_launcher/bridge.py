"""Мост launcher -> operator_ui_node: проба, команды, кадры /ws и прокси /ros/*."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 1.0
FRAME_MAX_AGE_S = 3.0
COMMAND_TIMEOUT_S = 15.0
CHUNK = 64 * 1024

_PASS_REQUEST_HEADERS = ("Range", "If-None-Match", "If-Modified-Since")
_PASS_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
)


def is_proxy_allowed(tail: str) -> bool:
    """Разрешённые GET-пути за /ros/: tours, media (манифест и файлы), ws."""
    if ".." in tail.split("/") or "\\" in tail:
        return False
    if tail in ("api/tours", "ws"):
        return True
    return tail.startswith(("api/media/", "media/")) and not tail.endswith("/")


@dataclass
class Frame:
    """Последний кадр состояния из /ws моста и момент его получения (monotonic)."""

    data: dict[str, Any]
    received_at: float


class Bridge:
    """HTTP-клиент к operator_ui_node с общим секретом в X-Bridge-Token."""

    def __init__(self, base_url: str, token: str) -> None:
        """`base_url` -- http://127.0.0.1:8091, `token` -- содержимое bridge_token_file."""
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """Общая клиентская сессия (создаётся лениво в работающем event loop)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(auto_decompress=False)
        return self._session

    async def close(self) -> None:
        """Закрыть клиентскую сессию."""
        if self._session is not None:
            await self._session.close()

    async def probe(self) -> bool:
        """GET /api/tours -> 200 за PROBE_TIMEOUT_S."""
        try:
            timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
            async with self.session.get(f"{self.base_url}/api/tours", timeout=timeout) as resp:
                await resp.read()
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def command(
        self, path: str, body: dict[str, Any] | None, operator: str
    ) -> tuple[int, Any]:
        """POST командного роута моста; возвращает (status, json-тело или {})."""
        headers = {"X-Bridge-Token": self._token, "X-Operator": operator}
        timeout = aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_S)
        try:
            async with self.session.post(
                f"{self.base_url}{path}", json=body or {}, headers=headers, timeout=timeout
            ) as resp:
                raw = await resp.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError:
                    payload = {"error": "bad_upstream_body"}
                return resp.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("bridge command %s failed: %r", path, exc)
            return 502, {"error": "bridge_unreachable"}

    async def get_json(self, path: str) -> tuple[int, Any]:
        """GET открытого роута моста (например /api/tours) с разбором JSON."""
        timeout = aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_S)
        try:
            async with self.session.get(f"{self.base_url}{path}", timeout=timeout) as resp:
                raw = await resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return 502, {"error": "bridge_unreachable"}

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        """Обработчик /ros/{tail}: только GET, allowlist, стриминг файлов, WS в обе стороны."""
        tail = request.match_info["tail"]
        if request.method not in ("GET", "HEAD"):
            return web.json_response({"error": "method_not_allowed"}, status=405)
        if not is_proxy_allowed(tail):
            return web.json_response({"error": "not_found"}, status=404)
        if tail == "ws":
            return await self._proxy_ws(request)
        return await self._proxy_get(request, tail)

    async def _proxy_get(self, request: web.Request, tail: str) -> web.StreamResponse:
        headers = {"Accept-Encoding": "identity"}
        for name in _PASS_REQUEST_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        timeout = aiohttp.ClientTimeout(total=None, connect=3.0, sock_read=30.0)
        try:
            async with self.session.get(
                f"{self.base_url}/{tail}",
                params=request.query,
                headers=headers,
                timeout=timeout,
            ) as up:
                resp = web.StreamResponse(status=up.status)
                for name in _PASS_RESPONSE_HEADERS:
                    if name in up.headers:
                        resp.headers[name] = up.headers[name]
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.debug("proxy GET %s failed: %r", tail, exc)
            return web.json_response({"error": "bridge_unreachable"}, status=502)

    async def _proxy_ws(self, request: web.Request) -> web.StreamResponse:
        try:
            upstream = await self.session.ws_connect(f"{self.base_url}/ws", heartbeat=20.0)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return web.json_response({"error": "bridge_unreachable"}, status=502)
        client = web.WebSocketResponse(heartbeat=20.0)
        await client.prepare(request)
        pumps = [
            asyncio.ensure_future(_pump(client, upstream)),
            asyncio.ensure_future(_pump(upstream, client)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            await upstream.close()
            await client.close()
        return client


async def _pump(src: Any, dst: Any) -> None:
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        else:
            break


class FrameWatcher:
    """Держит WS к мосту, пока стек UP, и хранит последний кадр состояния."""

    def __init__(
        self,
        bridge: Bridge,
        is_up: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_s: float = 1.0,
    ) -> None:
        """`is_up` -- колбэк состояния стека; вне UP соединение закрывается, кадр сбрасывается."""
        self._bridge = bridge
        self._is_up = is_up
        self._clock = clock
        self._retry_s = retry_s
        self.frame: Frame | None = None

    def latest(self) -> Frame | None:
        """Последний кадр или None (стек не UP, кадров нет или мост молчит > FRAME_MAX_AGE_S)."""
        frame = self.frame
        if frame is None or self._clock() - frame.received_at > FRAME_MAX_AGE_S:
            return None
        return frame

    async def run_forever(self) -> None:
        """Цикл подключения; исключения соединения не выходят наружу."""
        while True:
            if not self._is_up():
                self.frame = None
                await asyncio.sleep(self._retry_s / 2)
                continue
            try:
                await self._session_loop()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                log.debug("frame watcher: %r", exc)
            self.frame = None
            await asyncio.sleep(self._retry_s)

    async def _session_loop(self) -> None:
        async with self._bridge.session.ws_connect(f"{self._bridge.base_url}/ws") as ws:
            while self._is_up():
                try:
                    msg = await ws.receive(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    self.frame = Frame(data, self._clock())
