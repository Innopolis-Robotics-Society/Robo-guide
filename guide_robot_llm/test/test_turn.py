"""`dialog.turn.run_turn()` -- чистая логика на фейковых complete_*/speak/execute_tool.

Порядок фаз инвертирован: действие (GBNF+think) -> исполнение -> реплика ->
speak. Согласованность реплики с действием -- структурная: реплика
генерируется после исполнения и видит его итог.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from guide_robot_llm.dialog.turn import render_action_outcome, run_turn
from guide_robot_llm.llm_client import CompletionResult
from guide_robot_llm.llm_client.errors import BackendAborted, BackendTimeout

_TOOL_NAMES = ["guide_to", "noop"]
_ACTION_INSTRUCTION = "ACTION_INSTRUCTION_TEXT"
_ANSWER_INSTRUCTION = "ANSWER_INSTRUCTION_TEXT"


@dataclass
class _FakeResult:
    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


def _answer(text: str):
    def _complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        return CompletionResult(text=text)

    return _complete_answer


def _actions(*responses: str):
    calls = list(responses)

    def _complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=calls.pop(0))

    return _complete_action


def _noop_call(think: str = "поболтать") -> str:
    return json.dumps({"think": think, "tool": "noop", "args": {}})


def _guide_call(location_id: object = "cafe", think: str = "просит отвести") -> str:
    return json.dumps({"think": think, "tool": "guide_to", "args": {"location_id": location_id}})


def _run(**overrides):
    kwargs = {
        "system_prompt": "sys",
        "history_messages": [],
        "user_content": "user",
        "complete_answer": _answer("Привет!"),
        "complete_action": _actions(_noop_call()),
        "speak": lambda text: _FakeResult(ok=True),
        "execute_tool": lambda name, args: _FakeResult(ok=True),
        "tool_names": _TOOL_NAMES,
        "action_instruction": _ACTION_INSTRUCTION,
        "answer_instruction": _ANSWER_INSTRUCTION,
    }
    kwargs.update(overrides)
    return run_turn(**kwargs)


def test_action_selected_and_executed_before_answer_is_generated() -> None:
    order: list[str] = []

    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        order.append("action")
        return CompletionResult(text=_guide_call())

    def execute_tool(name: str, args: dict) -> _FakeResult:
        del args
        order.append(f"execute:{name}")
        return _FakeResult(ok=True)

    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        order.append("answer")
        return CompletionResult(text="Веду вас.")

    def speak(text: str) -> _FakeResult:
        del text
        order.append("speak")
        return _FakeResult(ok=True)

    result = _run(
        complete_action=complete_action,
        execute_tool=execute_tool,
        complete_answer=complete_answer,
        speak=speak,
    )

    assert order == ["action", "execute:guide_to", "answer", "speak"]
    assert result.stopped_reason == "ok"
    assert result.say_ok is True


def test_think_is_parsed_into_action_record() -> None:
    result = _run(complete_action=_actions(_guide_call(think="хочет к кафе")))
    assert result.action is not None
    assert result.action.think == "хочет к кафе"


def test_missing_think_is_tolerated_as_empty() -> None:
    legacy = json.dumps({"tool": "noop", "args": {}})
    result = _run(complete_action=_actions(legacy))
    assert result.action is not None
    assert result.action.name == "noop"
    assert result.action.think == ""


def test_answer_prompt_contains_action_outcome_after_static_instruction() -> None:
    seen_messages: list[list[dict]] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        seen_messages.append(messages)
        return CompletionResult(text="Веду вас.")

    result = _run(
        complete_action=_actions(_guide_call()),
        complete_answer=complete_answer,
    )

    answer_prompt = seen_messages[0][-1]
    assert answer_prompt["role"] == "user"
    # Статичная инструкция ПЕРВОЙ, волатильный итог -- хвостом (CACHE_REUSE).
    assert answer_prompt["content"].startswith(_ANSWER_INSTRUCTION)
    assert "Итог действия: " + render_action_outcome(result.action) in answer_prompt["content"]
    assert "выполнено: guide_to(location_id='cafe')" in answer_prompt["content"]


def test_failed_action_outcome_reaches_answer_prompt() -> None:
    seen_messages: list[list[dict]] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        seen_messages.append(messages)
        return CompletionResult(text="Не получилось.")

    result = _run(
        complete_action=_actions(_guide_call()),
        execute_tool=lambda name, args: _FakeResult(ok=False, message="нет такой локации"),
        complete_answer=complete_answer,
        repair_attempts=0,
    )

    assert result.stopped_reason == "action_invalid"
    assert result.say_ok is True  # реплика генерируется и при провале действия
    assert "не удалось: guide_to(location_id='cafe') — нет такой локации" in (
        seen_messages[0][-1]["content"]
    )


def test_empty_answer_after_sanitize_does_not_call_speak() -> None:
    spoken: list[str] = []

    result = _run(
        complete_answer=_answer("   "),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )

    assert spoken == []
    assert result.say_ok is False
    assert result.answer_text == ""


def test_markdown_from_answer_does_not_reach_speak() -> None:
    spoken: list[str] = []
    raw = "# Заголовок\n- пункт *важный*."

    result = _run(
        complete_answer=_answer(raw),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )

    assert spoken == ["Заголовок пункт важный."]
    # answer_raw_text -- то, что модель ДЕЙСТВИТЕЛЬНО сгенерировала, до
    # санитайзера: обязано отличаться от того, что ушло в speak()/answer_text.
    assert result.answer_raw_text == raw
    assert result.answer_text != result.answer_raw_text


def test_noop_does_not_call_execute_tool() -> None:
    executed: list[str] = []

    result = _run(
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "noop"
    assert result.stopped_reason == "ok"


def test_successful_action_is_terminal() -> None:
    result = _run(
        complete_action=_actions(_guide_call()),
        complete_answer=_answer("иду"),
        execute_tool=lambda name, args: _FakeResult(ok=True, message="ok"),
    )

    assert result.stopped_reason == "ok"
    assert result.action.name == "guide_to"
    assert result.action.result_ok is True
    assert result.repair_used is False


def test_repair_happens_exactly_once_then_succeeds() -> None:
    bad = _guide_call(location_id=1)
    good = _guide_call(location_id="cafe")
    executed: list[dict] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append(args)
        if args.get("location_id") == 1:
            return _FakeResult(ok=False, message="location_id: не задан(а)")
        return _FakeResult(ok=True, message="ok")

    result = _run(
        complete_action=_actions(bad, good),
        complete_answer=_answer("иду"),
        execute_tool=execute_tool,
        repair_attempts=1,
    )

    assert len(executed) == 2
    assert result.repair_used is True
    assert result.stopped_reason == "ok"
    assert result.action.result_ok is True


def test_repair_is_invisible_to_speak() -> None:
    """Починка происходит ДО фазы реплики -- speak() зовётся ровно один раз, после неё."""
    bad = _guide_call(location_id=1)
    good = _guide_call(location_id="cafe")
    spoken: list[str] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        if args.get("location_id") == 1:
            return _FakeResult(ok=False, message="location_id: не задан(а)")
        return _FakeResult(ok=True)

    _run(
        complete_action=_actions(bad, good),
        complete_answer=_answer("веду"),
        execute_tool=execute_tool,
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
        repair_attempts=1,
    )

    assert spoken == ["веду"]


def test_repair_exhausted_stops_with_action_invalid() -> None:
    bad = _guide_call(location_id=1)

    result = _run(
        complete_action=_actions(bad, bad),
        complete_answer=_answer("не вышло"),
        execute_tool=lambda name, args: _FakeResult(ok=False, message="плохо"),
        repair_attempts=1,
    )

    assert result.stopped_reason == "action_invalid"
    assert result.repair_used is True
    assert result.action.result_ok is False


def test_repair_attempts_zero_means_no_second_try() -> None:
    executed: list[dict] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append(args)
        return _FakeResult(ok=False, message="x")

    result = _run(
        complete_action=_actions(json.dumps({"think": "", "tool": "guide_to", "args": {}})),
        complete_answer=_answer("не вышло"),
        execute_tool=execute_tool,
        repair_attempts=0,
    )

    assert len(executed) == 1
    assert result.repair_used is False
    assert result.stopped_reason == "action_invalid"


def test_action_parse_error_stops_turn_without_speech() -> None:
    broken = "это не json"
    spoken: list[str] = []
    answer_calls: list[str] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        answer_calls.append("called")
        return CompletionResult(text="иду")

    result = _run(
        complete_action=_actions(broken),
        complete_answer=complete_answer,
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )

    assert result.stopped_reason == "action_parse_error"
    assert result.action is None
    assert spoken == []
    assert answer_calls == []
    # Сырой ответ модели сохраняется и в отдельном поле, и в транскрипте --
    # единственная улика, что модель вообще ответила, и чем именно.
    assert result.action_raw_text == broken
    assert result.messages[-1] == {"role": "assistant", "content": broken}


def test_finish_reason_propagates_from_both_phases() -> None:
    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        return CompletionResult(text="иду", finish_reason="stop")

    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=_noop_call(), finish_reason="stop")

    result = _run(complete_answer=complete_answer, complete_action=complete_action)

    assert result.answer_finish_reason == "stop"
    assert result.action_finish_reason == "stop"


def test_action_raw_text_reflects_last_repair_attempt_not_first() -> None:
    bad = _guide_call(location_id=1)
    good = _guide_call(location_id="cafe")

    def execute_tool(name: str, args: dict) -> _FakeResult:
        if args.get("location_id") == 1:
            return _FakeResult(ok=False, message="location_id: не задан(а)")
        return _FakeResult(ok=True, message="ok")

    result = _run(
        complete_action=_actions(bad, good),
        complete_answer=_answer("иду"),
        execute_tool=execute_tool,
        repair_attempts=1,
    )

    assert result.action_raw_text == good


def test_answer_backend_error_after_action_executed() -> None:
    """Бэкенд упал на фазе реплики: действие УЖЕ исполнено, но ничего не сказано."""
    spoken: list[str] = []
    executed: list[str] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        raise BackendTimeout("timed out")

    result = _run(
        complete_action=_actions(_guide_call()),
        complete_answer=complete_answer,
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )

    assert executed == ["guide_to"]
    assert spoken == []
    assert result.stopped_reason == "answer_backend_error"
    assert result.answer_text == ""
    assert result.action is not None  # действие в записи сохраняется


def test_action_backend_error_means_nothing_happened() -> None:
    spoken: list[str] = []
    executed: list[str] = []

    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        raise BackendTimeout("timed out")

    result = _run(
        complete_action=complete_action,
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )

    assert result.stopped_reason == "action_backend_error"
    assert executed == []
    assert spoken == []
    assert result.say_ok is False


def test_backend_aborted_in_action_phase_propagates() -> None:
    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        raise BackendAborted("barge-in")

    with pytest.raises(BackendAborted):
        _run(complete_action=complete_action)


def test_backend_aborted_in_answer_phase_propagates() -> None:
    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        raise BackendAborted("barge-in")

    with pytest.raises(BackendAborted):
        _run(complete_answer=complete_answer)


def test_check_aborted_stops_before_execute_tool() -> None:
    """Barge-in между выбором действия и исполнением: устаревшее действие не исполняется."""
    executed: list[str] = []
    spoken: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call()),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
        check_aborted=lambda: True,
    )

    assert executed == []
    assert spoken == []
    assert result.stopped_reason == "aborted"


def test_check_aborted_stops_before_speak() -> None:
    """Abort взводится после исполнения, но до speak(): говорить уже нельзя."""
    checks = iter([False, True])  # 1-й: перед execute_tool; 2-й: перед speak
    spoken: list[str] = []
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call()),
        complete_answer=_answer("веду"),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
        check_aborted=lambda: next(checks),
    )

    assert executed == ["guide_to"]
    assert spoken == []
    assert result.stopped_reason == "aborted"
    assert result.answer_text == "веду"


def test_check_aborted_not_called_before_speak_when_answer_is_empty() -> None:
    checks: list[bool] = []

    def check_aborted() -> bool:
        checks.append(True)
        return False

    _run(complete_answer=_answer(""), check_aborted=check_aborted)

    # noop не зовёт execute_tool, ответ пуст -- ни одной проверки не нужно.
    assert checks == []


def test_messages_layout_action_first_then_answer() -> None:
    history = [{"role": "user", "content": "СОБЫТИЕ: перешёл в IDLE"}]

    result = _run(
        history_messages=history,
        user_content="[состояние: IDLE]\nотведи меня к кафе",
        complete_action=_actions(_guide_call()),
        complete_answer=_answer("веду"),
    )

    roles = [m["role"] for m in result.messages]
    contents = [m["content"] for m in result.messages]
    assert roles[0] == "system"
    assert roles[1] == "user"  # история
    assert roles[2] == "user"  # реплика посетителя + статус
    assert contents[3] == _ACTION_INSTRUCTION
    assert roles[4] == "assistant"  # сырой tool-call JSON
    assert contents[5].startswith(_ANSWER_INSTRUCTION)  # инструкция реплики + итог
    assert roles[6] == "assistant"  # сырая реплика


def test_render_action_outcome_noop_and_failure() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    assert render_action_outcome(None) == "noop (никакого действия не выполнялось)"
    noop = ToolCallRecord(name="noop", args={}, result_ok=True, result_message="", result_data={})
    assert render_action_outcome(noop) == "noop (никакого действия не выполнялось)"
    failed = ToolCallRecord(
        name="guide_to",
        args={"location_id": "cafe"},
        result_ok=False,
        result_message="нет локации",
        result_data={},
    )
    expected = "не удалось: guide_to(location_id='cafe') — нет локации"
    assert render_action_outcome(failed) == expected
