"""HTTP-клиент к OpenAI-совместимому inference-серверу (llm_plam.md §4, шаг 4).

Чистая логика без `rclpy` -- тот же принцип пакета, что `matching.py`/
`snapshot.py`/`tools/`: тестируется без ROS, `dialog_agent_node.py` (шаг 5)
единственный потребитель из rclpy-контекста.
"""

from __future__ import annotations

from guide_robot_llm.llm_client.backend import Backend, BackendConfig, CompletionResult
from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)
from guide_robot_llm.llm_client.grammar import build_tool_call_grammar
from guide_robot_llm.llm_client.ladder import complete_with_fallback

__all__ = [
    "Backend",
    "BackendAborted",
    "BackendConfig",
    "BackendError",
    "BackendHTTPError",
    "BackendTimeout",
    "CompletionResult",
    "build_tool_call_grammar",
    "complete_with_fallback",
]
