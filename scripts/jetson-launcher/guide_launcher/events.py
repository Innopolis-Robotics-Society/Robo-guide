"""Single upstream fan-out, bounded operation registry and snapshot recovery."""

import asyncio
import time
import uuid
from collections import OrderedDict

from aiohttp import web

TERMINAL = {"succeeded", "failed", "canceled", "rejected", "unknown"}


class Events:
    """Fan out a single upstream snapshot and retain instance-scoped operations."""

    def __init__(self, monitor, watcher, limit=256):
        """Create a fresh instance and bounded registry."""
        self.instance_id = str(uuid.uuid4())
        self.monitor, self.watcher = monitor, watcher
        self.operations = OrderedDict()
        self.keys = {}
        self.limit = limit
        self.seq = 0
        self.clients = set()
        self.previous = None
        self.bridge_instance = None
        self.transport_generation = getattr(watcher, "generation", 0)
        self.tasks = set()

    def snapshot(self):
        """Separate transport connectivity from freshness and subsystem state."""
        frame = self.watcher.latest()
        robot = {k: v for k, v in frame.data.items() if k != "operations"} if frame else None
        if robot and (
            robot.get("mission_state_age_s") is None or robot["mission_state_age_s"] > 3
        ):
            robot["available_commands"] = []
        if robot and robot.get("localization", {}).get("motion_blocked"):
            blocked = {"missions", "missions/go-to", "missions/go-to-pose", "missions/return", "missions/base", "missions/resume", "missions/redirect"}
            robot["available_commands"] = [c for c in robot.get("available_commands", []) if c not in blocked]
        return {
            "instance_id": self.instance_id,
            "stack": self.monitor.status(),
            "transport_connected": getattr(self.watcher, "connected", False),
            "data_fresh": frame is not None,
            "robot": robot,
            "operations": [self.public(op) for op in self.operations.values()],
        }

    @staticmethod
    def public(op):
        """Remove internal owner and payload fields from an operation."""
        return {k: v for k, v in op.items() if not k.startswith("_")}

    def reserve(self, kind, payload, key, owner):
        """Deduplicate an operation before sending any ROS command."""
        identity = (owner, key)
        if identity in self.keys:
            op = self.operations[self.keys[identity]]
            if op["_payload"] != payload or op["kind"] != kind:
                raise ValueError("idempotency_conflict")
            return op, False
        # Never evict idempotency keys in this instance: reject when full.
        if len(self.operations) >= self.limit:
            raise ValueError("operation_capacity")
        op_id = str(uuid.uuid4())
        op = {
            "id": op_id,
            "instance_id": self.instance_id,
            "kind": kind,
            "status": "pending",
            "feedback": None,
            "result": None,
            "created_at": time.time(),
            "_payload": payload,
            "_owner": owner,
        }
        self.operations[op_id] = op
        self.keys[identity] = op_id
        return op, True

    def spawn(self, coro):
        """Track background command submission for shutdown."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def reconcile(self):
        """Merge upstream operations, marking interrupted work unknown."""
        frame = self.watcher.latest()
        generation = getattr(self.watcher, "generation", 0)
        disconnected = generation != self.transport_generation
        self.transport_generation = generation
        if frame is None or disconnected:
            for op in self.operations.values():
                if not op.get("_host") and op["status"] not in TERMINAL:
                    op["status"] = "unknown"
            if frame is None:
                return
        instance = frame.data.get("bridge_instance_id")
        if self.bridge_instance and instance != self.bridge_instance:
            for op in self.operations.values():
                if not op.get("_host") and op["status"] not in TERMINAL:
                    op["status"] = "unknown"
        self.bridge_instance = instance
        for incoming in frame.data.get("operations", []):
            op = self.operations.get(incoming["id"])
            if op and op["status"] not in TERMINAL:
                op.update(
                    {
                        k: v
                        for k, v in incoming.items()
                        if k in ("status", "feedback", "result", "goal_id", "updated_at")
                    }
                )

        readiness = frame.data.get("readiness", {})
        for op in self.operations.values():
            if op.get("_host"):
                continue
            subsystem = (
                "pose"
                if op["kind"] == "missions/go-to-pose"
                else "mission"
                if op["kind"].startswith("missions")
                else "speech"
            )
            if op["status"] not in TERMINAL and readiness.get(subsystem) is False:
                op["status"] = "unknown"

    def envelope(self, kind, data):
        """Assign instance identity and an increasing event sequence."""
        self.seq += 1
        return {"type": kind, "instance_id": self.instance_id, "seq": self.seq, "data": data}

    async def ws(self, request):
        """Send a new snapshot on every client connection."""
        self.reconcile()
        sock = web.WebSocketResponse(heartbeat=20)
        await sock.prepare(request)
        try:
            await sock.send_json(self.envelope("snapshot", self.snapshot()))
            self.clients.add(sock)
            async for _ in sock:
                pass
        finally:
            self.clients.discard(sock)
        return sock

    async def run(self):
        """Coalesce updates at one hertz and heartbeat unchanged snapshots."""
        while True:
            self.reconcile()
            state = self.snapshot()
            # Ignore transport timestamps when deciding whether semantic state changed.
            import copy

            comparable = copy.deepcopy(state)
            for field in ("seq", "stamp", "mission_state_age_s"):
                if comparable.get("robot"):
                    comparable["robot"].pop(field, None)
            message = self.envelope("state" if comparable != self.previous else "heartbeat", state)
            self.previous = comparable
            await asyncio.gather(*(self._send(sock, message) for sock in list(self.clients)))
            await asyncio.sleep(1)

    async def _send(self, sock, message):
        try:
            await asyncio.wait_for(sock.send_json(message), 1)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            self.clients.discard(sock)
            try:
                await asyncio.wait_for(sock.close(), 1)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        """Close clients and cancel outstanding submissions."""
        for sock in list(self.clients):
            await sock.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
