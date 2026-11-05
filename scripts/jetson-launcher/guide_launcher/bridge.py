"""Приватный транспорт хостового сервиса к guide_robot_api."""

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


@dataclass
class Frame:
    """Последний кадр состояния из /ws моста и момент его получения (monotonic)."""

    data: dict[str, Any]
    received_at: float


class Bridge:
    """HTTP-клиент к guide_robot_api с общим секретом в X-Bridge-Token."""

    def __init__(self, base_url: str, token: str) -> None:
        """`base_url` -- http://127.0.0.1:8091, `token` -- содержимое bridge_token_file."""
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """Общая клиентская сессия (создаётся лениво в работающем event loop)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auto_decompress=False, headers={"X-Bridge-Token": self._token}
            )
        return self._session

    async def close(self) -> None:
        """Закрыть клиентскую сессию."""
        if self._session is not None:
            await self._session.close()

    async def probe(self) -> bool:
        """GET /healthz -> 200 за PROBE_TIMEOUT_S."""
        try:
            timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
            async with self.session.get(f"{self.base_url}/healthz", timeout=timeout) as resp:
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
                    if resp.status >= 400:
                        return resp.status, {"message": f"upstream_http_{resp.status}"}
                    return 504, {
                        "error": "outcome_unknown",
                        "message": "invalid_upstream_response",
                    }
                if not isinstance(payload, dict):
                    return 504, {
                        "error": "outcome_unknown",
                        "message": "invalid_upstream_response",
                    }
                return resp.status, payload
        except aiohttp.ClientConnectorError:
            return 503, {"error": "subsystem_unavailable"}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("bridge command %s outcome unknown: %r", path, exc)
            return 504, {"error": "outcome_unknown"}

    async def get_json(self, path: str) -> tuple[int, Any]:
        """GET открытого роута моста (например /internal/tours) с разбором JSON."""
        timeout = aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_S)
        try:
            async with self.session.get(f"{self.base_url}{path}", timeout=timeout) as resp:
                raw = await resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return 502, {"error": "bridge_unreachable"}

    async def file(self, request):
        """Stream an allowed media path from the private bridge."""
        tail = request.match_info["tail"]
        if ".." in tail.split("/") or "\\" in tail:
            raise web.HTTPNotFound()
        return await self._proxy_get(request, "media/" + tail)

    async def _proxy_get(self, request: web.Request, tail: str) -> web.StreamResponse:
        headers = {"Accept-Encoding": "identity"}
        for name in _PASS_REQUEST_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        timeout = aiohttp.ClientTimeout(total=None, connect=3.0, sock_read=30.0)
        resp = None
        try:
            async with self.session.get(
                f"{self.base_url}/{tail}",
                params=request.query,
                headers=headers,
                timeout=timeout,
            ) as up:
                if up.status >= 400:
                    return web.json_response({"message": "media_unavailable"}, status=up.status)
                resp = web.StreamResponse(status=up.status)
                resp.headers["Cache-Control"] = "private, max-age=30"
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
            if resp is not None and resp.prepared:
                if request.transport is not None:
                    request.transport.close()
                return resp
            return web.json_response({"error": "bridge_unreachable"}, status=502)


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
        self.connected = False
        self.generation = 0
        self.frame: Frame | None = None

    def latest(self) -> Frame | None:
        """Последний кадр или None (стек не UP, кадров нет или мост молчит > FRAME_MAX_AGE_S)."""
        frame = self.frame
        if frame is None:
            return None
        elapsed = max(0.0, self._clock() - frame.received_at)
        if elapsed > FRAME_MAX_AGE_S:
            return None
        data = dict(frame.data)
        age = data.get("mission_state_age_s")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            data["mission_state_age_s"] = age + elapsed
        return Frame(data, frame.received_at)

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
            finally:
                self.connected = False
                self.frame = None
                self.generation += 1
            await asyncio.sleep(self._retry_s)

    async def _session_loop(self) -> None:
        async with self._bridge.session.ws_connect(f"{self._bridge.base_url}/internal/ws") as ws:
            self.connected = True
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
