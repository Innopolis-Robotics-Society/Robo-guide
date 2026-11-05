"""Private loopback transport; public contract belongs to the host gateway."""

import asyncio
import hmac
from pathlib import Path

from aiohttp import web


class ApiServer:
    """Private loopback server shared by the host connection."""

    def __init__(self, *, media_root, bridge_token, dispatch):
        """Create routes; media are optional."""
        self.dispatch = dispatch
        self.token = bridge_token
        self.clients = set()
        self.last = None
        self.runner = None
        self.app = web.Application(middlewares=[self.gate])
        self.app.router.add_get("/healthz", self.health)
        self.app.router.add_get("/internal/ws", self.ws)
        self.app.router.add_route("*", "/internal/{resource:.*}", self.handle)
        if Path(media_root).is_dir():
            self.app.router.add_static("/media", media_root)
        self.app.on_response_prepare.append(self.no_cache)

    @web.middleware
    async def gate(self, request, handler):
        """Require the internal secret for every route except liveness."""
        if request.path != "/healthz" and not hmac.compare_digest(
            request.headers.get("X-Bridge-Token", "").encode(), self.token.encode()
        ):
            return web.json_response({"message": "forbidden"}, status=403)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Private transport failure")
            return web.json_response({"message": "internal_error"}, status=500)

    async def health(self, request):
        """Report transport liveness without querying semantic services."""
        return web.json_response({"alive": True})

    async def handle(self, request):
        """Validate the request envelope and invoke the ROS adapter."""
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                raise ValueError("Expected object")
        except (ValueError, TypeError) as exc:
            return web.json_response({"message": str(exc)}, status=400)
        try:
            status, data = await self.dispatch(
                request.method, request.match_info["resource"], body
            )
            return (
                data
                if isinstance(data, web.StreamResponse)
                else web.json_response(data, status=status)
            )
        except (TimeoutError, asyncio.TimeoutError):
            return web.json_response({"message": "outcome_unknown"}, status=504)

    async def ws(self, request):
        """Replay the latest snapshot and retain one upstream subscriber."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            if self.last is not None:
                await ws.send_json(self.last)
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)
        return ws

    async def push(self, frame):
        """Broadcast using a stable client snapshot and bounded writes."""
        self.last = frame
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_json(frame), 1)
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                self.clients.discard(ws)

    async def start(self, host, port):
        """Bind the HTTP listener."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()

    async def stop(self):
        """Close clients before cleaning up the runner."""
        for ws in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    @staticmethod
    async def no_cache(request, response):
        """Prevent storage of private responses."""
        response.headers["Cache-Control"] = "no-cache"
