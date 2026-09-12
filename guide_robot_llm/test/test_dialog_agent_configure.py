"""`DialogAgentNode.on_configure()` -- отказ конфигурации без полного mission-стека.

`ToolBrokerTestHarness` сам ассертит успешный `trigger_configure()` в
`__init__` (`test/mocks/harness.py`) -- он не годится для сценария, где
конфигурация ДОЛЖНА провалиться. `_configure()` (TASK_external_llm_backend.md
§4) не трогает `tool_broker`/mission-стек, поэтому здесь достаточно голого
`rclpy.Context` с одной нодой.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import rclpy
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter

from guide_robot_llm.dialog_agent_node import DialogAgentNode


def test_missing_api_key_env_fails_configure() -> None:
    context = rclpy.Context()
    rclpy.init(context=context)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    prompt_file.write("Тестовый системный промпт.")
    prompt_file.close()
    node = DialogAgentNode(
        context=context,
        parameter_overrides=[
            Parameter("llm.backends", value=["selectel"]),
            Parameter("llm.selectel.base_url", value="http://127.0.0.1:1"),
            Parameter("llm.selectel.api_key_env", value="GUIDE_ROBOT_TEST_UNSET_KEY"),
            Parameter("system_prompt_path", value=prompt_file.name),
        ],
    )
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)
        Path(prompt_file.name).unlink(missing_ok=True)


def test_configure_succeeds_when_api_key_env_present(monkeypatch) -> None:
    monkeypatch.setenv("GUIDE_ROBOT_TEST_PRESENT_KEY", "sk-fake")
    context = rclpy.Context()
    rclpy.init(context=context)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    prompt_file.write("Тестовый системный промпт.")
    prompt_file.close()
    node = DialogAgentNode(
        context=context,
        parameter_overrides=[
            Parameter("llm.backends", value=["selectel"]),
            Parameter("llm.selectel.base_url", value="http://127.0.0.1:1"),
            Parameter("llm.selectel.api_key_env", value="GUIDE_ROBOT_TEST_PRESENT_KEY"),
            Parameter("llm.selectel.structured", value="json_object"),
            Parameter("system_prompt_path", value=prompt_file.name),
        ],
    )
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
        assert node._prompt_gate is True  # noqa: SLF001 -- тестовая интроспекция
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)
        Path(prompt_file.name).unlink(missing_ok=True)


def test_invalid_extra_body_json_fails_configure() -> None:
    context = rclpy.Context()
    rclpy.init(context=context)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    prompt_file.write("Тестовый системный промпт.")
    prompt_file.close()
    node = DialogAgentNode(
        context=context,
        parameter_overrides=[
            Parameter("llm.backends", value=["local"]),
            Parameter("llm.local.base_url", value="http://127.0.0.1:1"),
            Parameter("llm.local.extra_body_json", value="не json"),
            Parameter("system_prompt_path", value=prompt_file.name),
        ],
    )
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)
        Path(prompt_file.name).unlink(missing_ok=True)
