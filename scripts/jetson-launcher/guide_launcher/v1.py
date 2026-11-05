"""UI-independent HTTP API v1."""

import asyncio
import json

from aiohttp import web

from . import validation as v
from .api import Api, mission_link_ok, tour_active
from .events import TERMINAL, Events

PREFIX = "/api/v1"
COMMANDS = {
    "missions/pause",
    "missions/resume",
    "missions/stop",
    "missions/return",
    "missions/redirect",
    "missions/base",
    "interaction/confirm",
    "interaction/question",
    "interaction/answer",
    "face/override",
    "localization/reset",
    "costmaps/clear",
}


class RobotApi(Api):
    """Public robot control contract, independent of reference UI preferences."""

    def __init__(self, *args, **kwargs):
        """Attach the event and operation registry to host dependencies."""
        super().__init__(*args, **kwargs)
        self.events = Events(self.monitor, self.watcher)
        self._control_lock = asyncio.Lock()

    def restart_guard(self):
        """Refuse restart unless fresh idle or confirmed stopped state is available."""
        self.events.reconcile()
        if any(
            op["kind"].startswith("missions") and op["status"] in ("pending", "running")
            for op in self.events.operations.values()
        ):
            return "mission_operation_active"
        frame = self.watcher.latest()
        stopped_at = self.monitor.confirmed_stopped_at
        stopped = (
            stopped_at is not None
            and 0 <= self.clock() - stopped_at <= 3
            and not self.monitor.checks.launch
            and not self.monitor.checks.bridge
        )
        if not stopped:
            if not mission_link_ok(frame):
                return "mission_state_stale"
            if tour_active(frame):
                return "robot_moving"
        return None

    def _controlled(self, handler):
        async def wrapped(request):
            async with self._control_lock:
                movement_paths = {"missions", "missions/go-to-pose", "missions/go-to",
                                  "missions/return", "missions/resume", "missions/redirect", "missions/base"}
                resource = request.path.removeprefix(PREFIX + "/")
                observed = self.watcher.latest()
                frame = observed.data if observed else {}
                if resource in movement_paths and frame.get("localization", {}).get("motion_blocked"):
                    raise web.HTTPConflict(reason="localization_required")
                settings = request.app.get("system_settings")
                if (
                    settings
                    and settings.lock.locked()
                    and not request.path.startswith(f"{PREFIX}/wifi")
                ):
                    raise web.HTTPConflict(reason="network_transition_in_progress")
                if self.monitor.transitioning and not request.path.startswith(f"{PREFIX}/stack/"):
                    raise web.HTTPConflict(reason="stack_transition_in_progress")
                return await handler(request)

        return wrapped

    def _with_state(self, handler):
        async def wrapped(request):
            response = await handler(request)
            data = json.loads(response.text)
            self.events.reconcile()
            data["state"] = self.events.snapshot()
            if response.status < 400:
                data["accepted"] = True
            return web.json_response(data, status=response.status)

        return wrapped

    async def stack_restart(self, request):
        """Recheck actual ROS ownership before allowing destructive stack maintenance."""
        if not self.monitor.control:
            return await super().stack_restart(request)
        reason = self.restart_guard()
        if reason:
            raise web.HTTPConflict(reason=reason)
        if self.watcher.latest() is not None:
            status, body = await self.bridge.command(
                "/internal/missions/check-idle", {}, request["operator"]
            )
            if status != 200 or body.get("success") is not True:
                raise web.HTTPConflict(reason="safe_idle_not_confirmed")
        return await super().stack_restart(request)

    def register(self, app):
        """Register only versioned public routes."""
        app["api"] = self
        add = app.router.add_route
        for name in ("challenge", "verify", "logout"):
            add("POST", f"{PREFIX}/auth/{name}", getattr(self, f"auth_{name}"))
        add("GET", f"{PREFIX}/auth/status", self.auth_status)
        for name in ("start", "restart"):
            add(
                "POST",
                f"{PREFIX}/stack/{name}",
                self._authed(self._controlled(getattr(self, f"stack_{name}"))),
            )
        add("GET", f"{PREFIX}/stack/log", self._authed(self.stack_log))
        add("GET", f"{PREFIX}/stack/status", self.stack_status)
        add("GET", f"{PREFIX}/health", self.health)
        add("GET", f"{PREFIX}/state", self.state)
        add("GET", f"{PREFIX}/events", self.events.ws)
        add("GET", f"{PREFIX}/face/expressions", self.expressions)
        add("GET", f"{PREFIX}/map/image", self.map_image)
        for resource in (
            "map",
            "localization/presets",
            "tours",
            "locations",
            "content/{id}",
            "media/{id}",
        ):
            add("GET", f"{PREFIX}/{resource}", self.catalog)
        add("GET", f"{PREFIX}/media-files/{{tail:.+}}", self.media_file)
        for resource in ("missions", "speech", "missions/go-to", "missions/go-to-pose"):
            add(
                "POST",
                f"{PREFIX}/{resource}",
                self._authed(self._controlled(self.start_operation)),
            )
        add("GET", f"{PREFIX}/operations/{{id}}", self._authed(self.operation))
        add("POST", f"{PREFIX}/operations/{{id}}/cancel", self._authed(self.cancel_operation))
        for resource in COMMANDS:
            handler = (
                self.command if resource == "missions/stop" else self._controlled(self.command)
            )
            add("POST", f"{PREFIX}/{resource}", self._authed(handler))
        add("POST", f"{PREFIX}/face/clear", self._authed(self._with_state(self.clear_face)))
        add("POST", f"{PREFIX}/public/start-tour", self._controlled(self.public_start))
        add("POST", f"{PREFIX}/public/stop", self._with_state(self.public_stop))
        add(
            "POST",
            f"{PREFIX}/public/confirm",
            self._controlled(self._with_state(self.public_confirm)),
        )

    async def health(self, request):
        """Return host liveness without a ROS dependency."""
        return web.json_response({"alive": True, "instance_id": self.events.instance_id})

    async def stack_status(self, request):
        """Return observed stack startup and health state."""
        return web.json_response(self.monitor.status())

    async def state(self, request):
        """Reconcile operations before returning a snapshot."""
        self.events.reconcile()
        return web.json_response(self.events.snapshot())

    async def expressions(self, request):
        """Read the installed ROS expression catalog."""
        status, body = await self.bridge.get_json("/internal/face/expressions")
        return web.json_response(body, status=status)

    async def catalog(self, request):
        """Read a catalog resource through the private transport."""
        resource = request.path[len(PREFIX) + 1 :]
        status, data = await self.bridge.get_json("/internal/" + resource)
        return web.json_response(data, status=status)

    async def map_image(self, request):
        """Proxy the current map image through the authenticated private transport."""
        return await self.bridge._proxy_get(request, "internal/map/image")

    async def media_file(self, request):
        """Stream media while preserving byte range semantics."""
        return await self.bridge.file(request)

    async def start_operation(self, request):
        """Validate an explicit mission or speech request."""
        body = await request.json()
        resource = request.path[len(PREFIX) + 1 :]
        if resource == "missions/go-to":
            v.object_fields(body, ("location_id",))
            body = {
                "location_ids": [v.text(body.get("location_id"), "location_id")],
                "greet": False,
                "narrate": False,
                "return_home": False,
            }
            resource = "missions"
        payload = (
            v.pose(body)
            if resource == "missions/go-to-pose"
            else v.mission(body)
            if resource == "missions"
            else v.speech(body)
        )
        return await self.launch(request, resource, payload, request["owner"])

    async def launch(self, request, resource, payload, owner):
        """Reserve the idempotency key before submitting an action."""
        key = v.text(request.headers.get("Idempotency-Key"), "Idempotency-Key", 128)
        self.events.reconcile()
        identity = (owner, key)
        if identity not in self.events.keys:
            if self.monitor.transitioning:
                raise web.HTTPConflict(reason="stack_transition_in_progress")
            frame = self.watcher.latest()
            readiness = (
                "pose"
                if resource == "missions/go-to-pose"
                else "mission"
                if resource.startswith("missions")
                else "speech"
            )
            if frame is None or not frame.data.get("readiness", {}).get(readiness):
                raise web.HTTPServiceUnavailable(reason="subsystem_unavailable")
        op, created = self.events.reserve(resource, payload, key, owner)
        if created:
            self.events.spawn(
                self.send_operation(resource, payload, op, request.get("operator", "public"))
            )
        return web.json_response(self.events.public(op), status=202)

    async def send_operation(self, resource, payload, op, owner):
        """Submit once; an ambiguous failure never implies success."""
        try:
            status, result = await self.bridge.command(
                "/internal/" + resource, {**payload, "_operation_id": op["id"]}, owner
            )
            if op["status"] in TERMINAL:
                return
            if status in (500, 502, 504):
                op["status"] = "unknown"
            elif status >= 400:
                op["status"], op["result"] = "rejected", result
            elif op["status"] in ("pending", "running"):
                op.update(
                    {
                        k: value
                        for k, value in result.items()
                        if k in ("status", "feedback", "result", "goal_id")
                    }
                )
        except Exception as exc:
            op["status"], op["result"] = "unknown", {"message": str(exc)}

    async def operation(self, request):
        """Read a retained operation from this host instance."""
        self.events.reconcile()
        op = self.events.operations.get(request.match_info["id"])
        if not op:
            raise web.HTTPNotFound()
        return web.json_response(self.events.public(op))

    async def cancel_operation(self, request):
        """Allow only the creating session to request action cancellation."""
        self.events.reconcile()
        op = self.events.operations.get(request.match_info["id"])
        if not op:
            raise web.HTTPNotFound()
        if op["_owner"] != request["owner"]:
            return web.json_response({"message": "operation_owner_required"}, status=403)
        if op["status"] not in ("pending", "running"):
            return web.json_response({"message": "operation_not_cancelable"}, status=409)
        status, data = await self.bridge.command(
            f"/internal/operations/{op['id']}/cancel", {}, request["operator"]
        )
        return web.json_response(data, status=status)

    async def command(self, request):
        """Validate and dispatch a service command; ROS owns state transitions."""
        resource = request.path[len(PREFIX) + 1 :]
        body = await request.json() if request.can_read_body else {}
        fields = {
            "missions/redirect": ("location_id",),
            "missions/base": ("location_id",),
            "interaction/confirm": ("answer",),
            "interaction/answer": ("outcome",),
            "face/override": ("state", "gaze_az", "duration_s"),
            "localization/reset": ("confirm", "pose", "map_version", "preset_id"),
        }
        v.object_fields(body, fields.get(resource, ()))
        if resource in ("missions/redirect", "missions/base"):
            v.text(body.get("location_id"), "location_id")
        if resource == "interaction/confirm" and body.get("answer") not in (
            "yes",
            "stop",
            "question",
        ):
            raise ValueError("Invalid answer")
        if resource == "interaction/answer" and body.get("outcome") not in (
            "resume",
            "skip",
            "end",
        ):
            raise ValueError("Invalid outcome")
        if resource == "localization/reset" and body.get("confirm") is not True:
            raise ValueError("Explicit confirm required")
        if resource == "localization/reset":
            if "pose" in body and "preset_id" in body:
                raise ValueError("Specify pose or preset_id")
            if "pose" in body:
                v.pose({key: body[key] for key in ("pose", "map_version") if key in body})
            if "preset_id" in body:
                v.text(body["preset_id"], "preset_id")
        if resource == "face/override":
            v.text(body.get("state"), "state")
            body["duration_s"] = float(v.number(body.get("duration_s"), "duration_s", 0.1, 60))
            body["gaze_az"] = float(v.number(body.get("gaze_az", 0), "gaze_az", -90, 90))
        status, data = await self.bridge.command(
            "/internal/" + resource, body, request["operator"]
        )
        self.events.reconcile()
        if status < 400:
            data = {"accepted": True, "response": data, "state": self.events.snapshot()}
        else:
            data = {**data, "state": self.events.snapshot()}
        return web.json_response(data, status=status)

    async def clear_face(self, request):
        """Release the temporary ROS expression override."""
        status, data = await self.bridge.command(
            "/internal/face/override",
            {"state": "", "duration_s": 0.0, "gaze_az": 0.0},
            request["operator"],
        )
        return web.json_response(data, status=status)

    async def public_start(self, request):
        """Visitor starts are disabled; use the authenticated missions endpoint."""
        raise web.HTTPForbidden(reason="operator_session_required")
