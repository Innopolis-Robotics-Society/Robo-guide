"""Base contract for supervisor watchdogs.

A watchdog is a small, stateless-from-outside probe: the supervisor creates it
once, calls ``check()`` periodically and reacts to the returned level according
to the policy declared in the YAML config.

To add a new watchdog:
  1. subclass :class:`WatchdogBase` in this package (or any importable module),
  2. implement ``setup()`` (optional) and ``check()``,
  3. reference it by dotted path in ``supervisor.yaml``.
No recompilation of the supervisor is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict


class Level(IntEnum):
    """Severity, deliberately aligned with diagnostic_msgs/DiagnosticStatus."""

    OK = 0
    WARN = 1
    ERROR = 2
    STALE = 3


@dataclass
class Status:
    level: Level
    message: str = ""
    values: Dict[str, str] = None

    def __post_init__(self) -> None:
        if self.values is None:
            self.values = {}

    @property
    def bad(self) -> bool:
        return self.level in (Level.ERROR, Level.STALE)


class WatchdogBase:
    """Common plumbing. Subclasses must implement :meth:`check`."""

    def __init__(self, name: str, node: Any, params: Dict[str, Any]) -> None:
        self.name = name
        self.node = node
        self.params = params or {}
        self.log = node.get_logger().get_child(name)
        self.setup()

    # -- lifecycle -----------------------------------------------------------
    def setup(self) -> None:
        """Create subscriptions/clients here. Called once at construction."""

    def reset(self) -> None:
        """Drop accumulated state (called after a successful recovery)."""

    def destroy(self) -> None:
        """Release ROS entities. Called on supervisor shutdown."""

    # -- probe ---------------------------------------------------------------
    def check(self) -> Status:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------
    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def now(self) -> float:
        return self.node.get_clock().now().nanoseconds * 1e-9
