"""Bounded action registry, mutated only on the asyncio thread."""

import time
from collections import OrderedDict


class ActionRegistry:
    """Bound action state and goal handles on the transport event loop."""

    def __init__(self, limit=256):
        """Create an empty registry with a hard capacity."""
        self.items = OrderedDict()
        self.handles = {}
        self.limit = limit

    def add(self, operation_id, kind):
        """Reserve an operation, evicting a completed entry when necessary."""
        if operation_id in self.items:
            return False
        if len(self.items) >= self.limit:
            removable = next(
                (
                    k
                    for k, v in self.items.items()
                    if v["status"] in ("succeeded", "failed", "canceled", "rejected")
                ),
                None,
            )
            if removable is None:
                raise ValueError("operation_capacity")
            self.items.pop(removable)
            self.handles.pop(removable, None)
        self.items[operation_id] = {
            "id": operation_id,
            "kind": kind,
            "status": "pending",
            "feedback": None,
            "result": None,
            "updated_at": time.time(),
        }
        return True

    def update(self, operation_id, **values):
        """Record feedback or outcome with its update time."""
        if operation_id in self.items:
            self.items[operation_id].update(values, updated_at=time.time())

    def snapshot(self):
        """Return the retained operations for the upstream snapshot."""
        return list(self.items.values())
