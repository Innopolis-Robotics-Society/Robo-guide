"""Authenticated host power requests with a fixed privileged helper."""

import asyncio
from pathlib import Path

from aiohttp import web

from . import validation as v
from .stack import default_run

MARKER = Path("/run/guide-system-power.pending")


class PowerControl:
    def __init__(self, api):
        self.api = api
        self.operation = None
        self.last_operation = None
        self.run = default_run
        self.lock = asyncio.Lock()

    @property
    def pending(self):
        return self.operation is not None or MARKER.exists()

    async def status(self, request):
        return web.json_response({
            "enabled": self.api.cfg.power_control_enabled,
            "pending": self.pending,
            "operation_id": self.last_operation["id"] if self.last_operation else None,
        })

    async def request(self, request):
        api = self.api
        action = request.match_info["action"]
        if action not in ("shutdown", "reboot"):
            raise web.HTTPNotFound()
        body = await request.json()
        v.object_fields(body, ("confirm",))
        if body.get("confirm") is not True:
            raise ValueError("Explicit confirm=true required")
        key = v.text(request.headers.get("Idempotency-Key"), "Idempotency-Key")
        async with self.lock:
            if not api.cfg.power_control_enabled:
                raise web.HTTPServiceUnavailable(reason="power_control_disabled")
            if self.operation is not None:
                # Registry enforces ownership and a matching request key.
                op, created = api.events.reserve("system_power", {"action": action}, key, request["owner"])
                if created:
                    op["status"] = "rejected"
                    op["_host"] = True
                    op["result"] = {"message": "system_power_pending"}
                    raise web.HTTPConflict(reason="system_power_pending")
                return web.json_response(api.events.public(op), status=202)
            if self.pending:
                raise web.HTTPConflict(reason="system_power_pending")
            op, created = api.events.reserve("system_power", {"action": action}, key, request["owner"])
            if created:
                op["_host"] = True
                self.operation = op
                self.last_operation = op
                api.events.spawn(self.execute(action, op))
            return web.json_response(api.events.public(op), status=202)

    async def execute(self, action, op):
        op["status"] = "running"
        op["feedback"] = {"phase": "scheduling"}
        result = await self.run([
            "sudo", "-n", "/usr/local/sbin/guide-system-power",
            "poweroff" if action == "shutdown" else "reboot",
        ], 10.0)
        if result.rc == 0:
            # Success means scheduling, never proof that power is already off.
            op["status"] = "succeeded"
            op["result"] = {"scheduled": True, "action": action, "delay_s": 0}
        else:
            op["status"] = "unknown" if result.rc == -1 else "failed"
            op["result"] = {"message": "outcome_unknown" if result.rc == -1 else "power_command_failed"}
            if result.rc != -1 and not MARKER.exists():
                self.operation = None

    def register(self, app):
        app.router.add_get("/api/v1/system/power", self.api._authed(self.status))
        app.router.add_post("/api/v1/system/{action}", self.api._authed(self.request))
