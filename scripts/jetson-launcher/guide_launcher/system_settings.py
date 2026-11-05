"""Host-owned audio and network API; privileged work stays behind Unix sockets."""

import asyncio
import hashlib
import hmac
import secrets

import aiohttp
from aiohttp import web

from . import validation as v


class SystemSettings:
    """Explicitly scoped clients for system helpers, independent of ROS and UI."""

    def __init__(self, api):
        """Initialize the adapter and its configured resources."""
        self.api = api
        self.lock = asyncio.Lock()
        self.fingerprint_key = secrets.token_bytes(32)

    async def call(self, kind, method, path, body=None):
        """Call only a configured local helper endpoint."""
        socket = getattr(self.api.cfg, kind + "_socket")
        if not socket:
            raise web.HTTPServiceUnavailable(reason=kind + "_unavailable")
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=socket)
            ) as session:
                async with session.request(
                    method,
                    "http://localhost" + path,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=150),
                ) as response:
                    data = await response.json()
                    return response.status, data
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            raise web.HTTPGatewayTimeout(reason="outcome_unknown") from None

    async def audio(self, request):
        """Validate and forward speaker settings."""
        body = None
        if request.method == "POST":
            body = await request.json()
            v.object_fields(body, ("volume", "muted"))
            if "volume" in body:
                v.number(body["volume"], "volume", 0, 100)
            if "muted" in body and not isinstance(body["muted"], bool):
                raise ValueError("Invalid muted")
        status, data = await self.call("audio", request.method, "/output", body)
        return web.json_response(data, status=status)

    async def wifi(self, request):
        """Authorize and serialize network changes."""
        action = request.match_info.get("action", "status")
        if action not in ("status", "scan", "connect", "disconnect", "forget"):
            raise web.HTTPNotFound()
        body = await request.json() if request.can_read_body else {}
        v.object_fields(body, ("ssid", "password", "profile_id", "hidden"))
        if action in ("connect", "disconnect", "forget"):
            if self.api.restart_guard():
                raise web.HTTPConflict(reason="safe_idle_not_confirmed")
            if self.api.watcher.latest() is not None:
                status, observed = await self.api.bridge.command(
                    "/internal/missions/check-idle", {}, request["operator"]
                )
                if status != 200 or observed.get("success") is not True:
                    raise web.HTTPConflict(reason="safe_idle_not_confirmed")
        if action == "connect":
            if not self.api.cfg.wifi_socket:
                raise web.HTTPServiceUnavailable(reason="wifi_unavailable")
            key = v.text(request.headers.get("Idempotency-Key"), "Idempotency-Key")
            password = body.get("password", "")
            if not isinstance(password, str):
                raise ValueError("Invalid password")
            identity = {k: value for k, value in body.items() if k != "password"}
            identity["credential_fingerprint"] = hmac.new(
                self.fingerprint_key, password.encode(), hashlib.sha256
            ).hexdigest()
            op, created = self.api.events.reserve("wifi", identity, key, request["owner"])
            op["_host"] = True
            if created:
                if self.lock.locked():
                    op["status"] = "rejected"
                    op["result"] = {"message": "network_busy"}
                else:
                    await self.lock.acquire()
                    self.api.events.spawn(self.connect(body, op))
            return web.json_response(self.api.events.public(op), status=202)
        if self.lock.locked():
            raise web.HTTPConflict(reason="network_busy")
        async with self.lock:
            status, data = await self.call("wifi", request.method, "/" + action, body)
        return web.json_response(data, status=status)

    async def connect(self, body, op):
        """Track connection separately; credentials live only in this running call."""
        op["status"] = "running"
        op["feedback"] = {"phase": "connecting"}
        async def progress():
            while True:
                await asyncio.sleep(1)
                try:
                    status, data = await self.call("wifi", "GET", "/progress")
                    if status == 200 and data.get("phase") in (
                        "connecting", "authenticating", "obtaining_address", "restoring"
                    ):
                        op["feedback"] = {"phase": data["phase"]}
                except web.HTTPException:
                    pass

        progress_task = asyncio.create_task(progress())
        try:
            status, result = await self.call("wifi", "POST", "/connect", body)
            op["status"] = (
                "succeeded" if status < 400 else "unknown" if status >= 500 else "failed"
            )
            op["result"] = result
        except web.HTTPException:
            op["status"] = "unknown"
            op["result"] = {"message": "outcome_unknown"}
        finally:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
            body.pop("password", None)
            self.lock.release()

    def register(self, app):
        """Register authenticated host settings routes."""
        app.router.add_route("GET", "/api/v1/audio/output", self.api._authed(self.audio))
        app.router.add_route("POST", "/api/v1/audio/output", self.api._authed(self.audio))
        app.router.add_get("/api/v1/wifi", self.api._authed(self.wifi))
        app.router.add_post(
            "/api/v1/wifi/{action}", self.api._authed(self.api._controlled(self.wifi))
        )
