"""HTTP-клиент к OpenAI-совместимому inference-серверу (llm_plam.md §4, шаг 4).

Чистая логика без `rclpy` -- тот же принцип пакета, что `matching.py`/
`snapshot.py`/`tools/`: тестируется без ROS, `dialog_agent_node.py` (шаг 5)
единственный потребитель из rclpy-контекста.
"""

from __future__ import annotations

from guide_robot_llm.llm_client.backend import (
    Backend,
    BackendConfig,
    CompletionResult,
    build_content,
    count_images,
    has_images,
)
from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)
from guide_robot_llm.llm_client.grammar import build_action_grammar
from guide_robot_llm.llm_client.ladder import complete_with_fallback
from guide_robot_llm.llm_client.telemetry import ClientTelemetry, StageTimings

__all__ = [
    "Backend",
    "BackendAborted",
    "BackendConfig",
    "BackendError",
    "BackendHTTPError",
    "BackendTimeout",
    "ClientTelemetry",
    "CompletionResult",
    "StageTimings",
    "build_action_grammar",
    "build_content",
    "complete_with_fallback",
    "count_images",
    "has_images",
]
