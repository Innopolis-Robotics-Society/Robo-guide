"""`dialog.loop.run_react_turn()` -- чистая логика на фейковых complete/execute_tool.

llm_plam.md §5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from guide_robot_llm.dialog.loop import run_react_turn
from guide_robot_llm.llm_client import CompletionResult
from guide_robot_llm.llm_client.errors import BackendAborted, BackendTimeout

_TOOL_NAMES = ["say", "list_locations"]


@dataclass
class _FakeResult:
    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


def _scripted_complete(*responses: str):
    """Фейковый `complete`: возвращает по одному заскриптованному ответу за вызов."""
    calls = list(responses)

    def _complete(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=calls.pop(0))

    return _complete


def test_terminal_tool_stops_turn_after_one_call() -> None:
    complete = _scripted_complete(json.dumps({"tool": "say", "args": {"text": "привет"}}))
    executed: list[tuple[str, dict]] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append((name, args))
        return _FakeResult(ok=True, message="ok")

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=execute_tool,
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "terminal_tool"
    assert len(result.calls) == 1
    assert result.calls[0].name == "say"
    assert executed == [("say", {"text": "привет"})]


def test_read_only_tool_is_not_terminal_and_continues() -> None:
    complete = _scripted_complete(
        json.dumps({"tool": "list_locations", "args": {}}),
        json.dumps({"tool": "say", "args": {"text": "нашёл"}}),
    )
    executed: list[str] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        del args
        executed.append(name)
        return _FakeResult(ok=True)

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=execute_tool,
        tool_names=_TOOL_NAMES,
        max_tool_calls=2,
    )

    assert executed == ["list_locations", "say"]
    assert result.stopped_reason == "terminal_tool"
    assert len(result.calls) == 2


def test_failed_terminal_tool_does_not_stop_turn_gives_retry_chance() -> None:
    """Регрессия: провалившийся start_tour(tour_id=1 int) не должен молча
    заканчивать ход -- модель обязана увидеть ошибку и получить шанс
    исправиться (воспроизведено вживую: маленькая модель прислала tour_id
    числом, GBNF типы не проверяет, а старая логика останавливала ход на
    первой же неудаче терминального инструмента)."""
    complete = _scripted_complete(
        json.dumps({"tool": "start_tour", "args": {"tour_id": 1}}),
        json.dumps({"tool": "start_tour", "args": {"tour_id": "lab_demo"}}),
    )
    executed: list[dict] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append(args)
        if args.get("tour_id") == 1:
            return _FakeResult(ok=False, message="тур: не задан(а)")
        return _FakeResult(ok=True, message="тур начат")

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=execute_tool,
        tool_names=["start_tour"],
        max_tool_calls=2,
    )

    assert len(executed) == 2
    assert result.stopped_reason == "terminal_tool"
    assert result.calls[0].result_ok is False
    assert result.calls[1].result_ok is True


def test_repeatedly_failing_terminal_tool_stops_at_max_calls_not_terminal() -> None:
    complete = _scripted_complete(
        json.dumps({"tool": "start_tour", "args": {"tour_id": 1}}),
        json.dumps({"tool": "start_tour", "args": {"tour_id": 2}}),
    )

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=False, message="плохо"),
        tool_names=["start_tour"],
        max_tool_calls=2,
    )

    assert result.stopped_reason == "max_calls"
    assert len(result.calls) == 2
    assert all(not c.result_ok for c in result.calls)


def test_max_tool_calls_stops_even_when_not_terminal() -> None:
    complete = _scripted_complete(
        json.dumps({"tool": "list_locations", "args": {}}),
        json.dumps({"tool": "list_locations", "args": {}}),
    )

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=_TOOL_NAMES,
        max_tool_calls=2,
    )

    assert result.stopped_reason == "max_calls"
    assert len(result.calls) == 2


def test_malformed_json_stops_turn_as_parse_error() -> None:
    complete = _scripted_complete("это не json вовсе")

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "parse_error"
    assert result.calls == []


def test_missing_tool_key_treated_as_parse_error() -> None:
    complete = _scripted_complete(json.dumps({"args": {}}))

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "parse_error"


def test_args_not_object_treated_as_parse_error() -> None:
    complete = _scripted_complete(json.dumps({"tool": "say", "args": "не объект"}))

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "parse_error"


def test_missing_args_defaults_to_empty_dict_not_an_error() -> None:
    """GBNF всегда требует "args", но вызывающий (execute_tool) не обязан падать,
    если модель однажды его опустит -- say/stop_tour и т.п. валидны с {}."""
    complete = _scripted_complete(json.dumps({"tool": "stop_tour"}))
    received: list[dict] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        received.append(args)
        return _FakeResult(ok=True)

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=execute_tool,
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "terminal_tool"
    assert received == [{}]


def test_backend_aborted_propagates_not_swallowed() -> None:
    def complete(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        raise BackendAborted("barge-in")

    with pytest.raises(BackendAborted):
        run_react_turn(
            system_prompt="sys",
            user_content="user",
            complete=complete,
            execute_tool=lambda name, args: _FakeResult(ok=True),
            tool_names=_TOOL_NAMES,
        )


def test_backend_error_stops_turn_without_raising() -> None:
    def complete(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        raise BackendTimeout("timed out")

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=_TOOL_NAMES,
    )

    assert result.stopped_reason == "backend_error"
    assert result.calls == []


def test_messages_log_grows_with_assistant_and_tool_result() -> None:
    complete = _scripted_complete(json.dumps({"tool": "say", "args": {"text": "hi"}}))

    result = run_react_turn(
        system_prompt="sys",
        user_content="user",
        complete=complete,
        execute_tool=lambda name, args: _FakeResult(ok=True, message="done"),
        tool_names=_TOOL_NAMES,
    )

    assert [m["role"] for m in result.messages] == ["system", "user", "assistant", "user"]
