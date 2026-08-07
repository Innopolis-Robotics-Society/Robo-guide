"""`dialog.prompt.build_system_prompt()`: преамбул + каталог (llm_plam.md §5)."""

from __future__ import annotations

from guide_robot_llm.dialog.prompt import build_system_prompt
from guide_robot_llm.tools.schema import ToolSpec

_SAY = ToolSpec("say", "Сказать реплику посетителю.", frozenset({0}))
_STOP = ToolSpec("stop_tour", "Прервать текущий тур совсем.", frozenset({1}))


def test_prompt_starts_with_preamble_verbatim() -> None:
    prompt = build_system_prompt("ПРЕАМБУЛА ТЕКСТ", [_SAY])
    assert prompt.startswith("ПРЕАМБУЛА ТЕКСТ")


def test_prompt_lists_exactly_given_tools_with_descriptions() -> None:
    prompt = build_system_prompt("x", [_SAY, _STOP])

    assert "- say: Сказать реплику посетителю." in prompt
    assert "- stop_tour: Прервать текущий тур совсем." in prompt


def test_prompt_omits_tools_not_passed() -> None:
    prompt = build_system_prompt("x", [_SAY])

    assert "stop_tour" not in prompt


def test_empty_tool_list_still_produces_valid_prompt() -> None:
    prompt = build_system_prompt("преамбула", [])

    assert prompt.startswith("преамбула")
