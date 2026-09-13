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
from guide_robot_llm.pointing import (
    CameraGeometry,
    Candidate,
    PointingContext,
    PointingEvidence,
    RobotPose,
)
from guide_robot_llm.tools.validate import (
    REASON_AMBIGUOUS_TARGET,
    REASON_NO_CANDIDATE,
    REASON_STALE_FRAMES,
)

_TOOL_NAMES = ["guide_to", "reply"]
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

    def _complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=calls.pop(0))

    return _complete_action


def _reply_call(confidence: float = 0.9, abstain: bool = False) -> str:
    return json.dumps(
        {"tool": "reply", "args": {}, "confidence": confidence, "abstain": abstain}
    )


def _guide_call(
    location_id: object = "cafe",
    confidence: float = 0.9,
    abstain: bool = False,
) -> str:
    return json.dumps(
        {
            "tool": "guide_to",
            "args": {"location_id": location_id},
            "confidence": confidence,
            "abstain": abstain,
        }
    )


def _run(**overrides):
    kwargs = {
        "system_prompt": "sys",
        "history_messages": [],
        "user_content": "user",
        "complete_answer": _answer("Привет!"),
        "complete_action": _actions(_reply_call()),
        "speak": lambda text: _FakeResult(ok=True),
        "execute_tool": lambda name, args: _FakeResult(ok=True),
        "tool_names": _TOOL_NAMES,
        "action_instruction": _ACTION_INSTRUCTION,
        "answer_instruction": _ANSWER_INSTRUCTION,
    }
    kwargs.update(overrides)
    return run_turn(**kwargs)


# ---------------------------------------------------------------------------
# Таига #7: resolve_pointing -- host-side геометрия ДО execute_tool
# ---------------------------------------------------------------------------

# Та же сцена, что в test_pointing.py: камера 1 м, смотрит вперёд (yaw 0 = +x),
# наклон 10°. Экспонаты на ~3 м впереди.
_PT_CAM = CameraGeometry(
    width_px=1280, height_px=720, hfov_deg=60, vfov_deg=34, mount_z=1.0, pitch_deg=10
)
_PT_POSE = RobotPose(x=0.0, y=0.0, yaw=0.0)


def _pt_cand(cid: str, name: str, x: float, y: float) -> Candidate:
    return Candidate(id=cid, name=name, x=x, y=y, aliases=())


def _pt_ctx(
    cands: list[Candidate],
    *,
    box=None,
    present: bool = True,
    quality: str = "ok",
    visible=(),
    utterance: str = "",
) -> PointingContext:
    return PointingContext(
        candidates=tuple(cands),
        robot_pose=_PT_POSE,
        camera=_PT_CAM,
        pointing=PointingEvidence(present=present, box=box),
        utterance=utterance,
        frame_quality=quality,
        visible_ids=frozenset(visible),
    )


def _pt_call(content_id: str = "robot", confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "tool": "resolve_pointing",
            "args": {"content_id": content_id},
            "confidence": confidence,
            "abstain": False,
        }
    )


# Сцены (см. test_pointing.py): резолвится / неоднозначно / без кандидата.
def _pt_ctx_resolved() -> PointingContext:
    return _pt_ctx(
        [_pt_cand("robot", "робот", 3.0, 0.0)],
        box=(0.45, 0.69, 0.55, 0.79),
        visible=("robot",),
        utterance="расскажи про этот",
    )


def _pt_ctx_ambiguous() -> PointingContext:
    return _pt_ctx(
        [
            _pt_cand("left", "синий робот", 3.0, -0.2),
            _pt_cand("right", "красный робот", 3.0, 0.2),
        ],
        box=(0.45, 0.70, 0.55, 0.80),
        visible=("left", "right"),
    )


def _pt_ctx_stale() -> PointingContext:
    return _pt_ctx(
        [_pt_cand("robot", "робот", 3.0, 0.0)],
        box=(0.45, 0.69, 0.55, 0.79),
        visible=("robot",),
        quality="stale",
    )


def _pt_ctx_no_candidate() -> PointingContext:
    # Жест есть, но правдоподобных видимых кандидатов нет (все вне кадра).
    return _pt_ctx(
        [_pt_cand("far", "далёкий", 50.0, 0.0)],
        box=(0.45, 0.69, 0.55, 0.79),
        visible=("far",),
    )


def test_action_selected_and_executed_before_answer_is_generated() -> None:
    order: list[str] = []

    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
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


def test_extra_think_key_is_malformed_and_repaired() -> None:
    """ADR-0001 §3: `think` из контракта убран -- чужой ключ = malformed."""
    bad = json.dumps(
        {"think": "хочет к кафе", "tool": "guide_to", "args": {"location_id": "cafe"}}
    )
    executed: list[str] = []

    result = _run(
        complete_action=_actions(bad, _guide_call()),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
    )

    assert executed == ["guide_to"]
    assert result.repair_used is True
    assert result.stopped_reason == "ok"
    assert result.action_first_attempt_valid is False


def test_legacy_two_field_action_is_malformed() -> None:
    """Старый 2-полевой формат ({tool, args}) без confidence/abstain -- malformed."""
    legacy = json.dumps({"tool": "reply", "args": {}})
    result = _run(complete_action=_actions(legacy, _reply_call()), repair_attempts=1)

    assert result.action is not None
    assert result.action.name == "reply"
    assert result.repair_used is True


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


def test_answer_prompt_anchors_the_visitor_utterance_after_outcome() -> None:
    """stage5 п.2: живой баг -- без якоря фаза 2 видела только «действий не
    требуется» и хвост своих же прошлых ответов, отвечала не на последнюю
    реплику. Якорь -- хвостом, ПОСЛЕ «Итог действия: ...» (правило кэша)."""
    seen_messages: list[list[dict]] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        seen_messages.append(messages)
        return CompletionResult(text="Сейчас два плюс два будет четыре.")

    _run(
        complete_answer=complete_answer,
        utterance="посчитай два плюс два",
    )

    answer_prompt = seen_messages[0][-1]["content"]
    outcome_pos = answer_prompt.index("Итог действия: ")
    utterance_pos = answer_prompt.index("Реплика посетителя: «посчитай два плюс два»")
    assert utterance_pos > outcome_pos
    assert "Ответь именно на неё." in answer_prompt


def test_answer_prompt_omits_utterance_block_when_empty() -> None:
    seen_messages: list[list[dict]] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        seen_messages.append(messages)
        return CompletionResult(text="Привет!")

    _run(complete_answer=complete_answer)

    assert "Реплика посетителя:" not in seen_messages[0][-1]["content"]


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
    assert (
        "не удалось: guide_to(location_id='cafe') — нет такой локации"
        in (seen_messages[0][-1]["content"])
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


def test_reply_does_not_call_execute_tool() -> None:
    executed: list[str] = []

    result = _run(
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
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
    """ADR-0001 §2: кривые аргументы режет ВАЛИДАТОР до брокера --
    `execute_tool` не видит invalid-вызов вовсе."""
    bad = _guide_call(location_id=1)
    good = _guide_call(location_id="cafe")
    executed: list[dict] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append(args)
        return _FakeResult(ok=True, message="ok")

    result = _run(
        complete_action=_actions(bad, good),
        complete_answer=_answer("иду"),
        execute_tool=execute_tool,
        repair_attempts=1,
        known_location_ids=frozenset({"cafe"}),
    )

    assert executed == [{"location_id": "cafe"}]
    assert result.repair_used is True
    assert result.stopped_reason == "ok"
    assert result.action.result_ok is True
    # Первая попытка была СХЕМА-валидна (отклонены аргументы, не конверт).
    assert result.action_first_attempt_valid is True


def test_repair_is_invisible_to_speak() -> None:
    """Починка происходит ДО фазы реплики -- speak() зовётся ровно один раз, после неё."""
    bad = _guide_call(location_id=1)
    good = _guide_call(location_id="cafe")
    spoken: list[str] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        return _FakeResult(ok=True)

    _run(
        complete_action=_actions(bad, good),
        complete_answer=_answer("веду"),
        execute_tool=execute_tool,
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
        repair_attempts=1,
        known_location_ids=frozenset({"cafe"}),
    )

    assert spoken == ["веду"]


def test_repair_exhausted_stops_with_action_invalid() -> None:
    """Схема-валидное действие, но брокер отвечает ошибкой: после исчерпания
    починки ход обрывается как `action_invalid` (реплика честно говорит о провале)."""
    bad = _guide_call(location_id="cafe")

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
        complete_action=_actions(_guide_call()),
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
        repair_attempts=0,  # без починки malformed сразу обрывает ход
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

    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=_reply_call(), finish_reason="stop")

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

    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
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
    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
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

    # reply не зовёт execute_tool, ответ пуст -- ни одной проверки не нужно.
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


def test_render_action_outcome_reply_and_failure() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    _REPLY_TEXT = (
        "Действий не требуется — просто ответь посетителю на его "
        "последнюю реплику, как живой собеседник."
    )
    assert render_action_outcome(None) == _REPLY_TEXT
    reply = ToolCallRecord(
        name="reply", args={}, result_ok=True, result_message="", result_data={}
    )
    assert render_action_outcome(reply) == _REPLY_TEXT
    failed = ToolCallRecord(
        name="guide_to",
        args={"location_id": "cafe"},
        result_ok=False,
        result_message="нет локации",
        result_data={},
    )
    expected = "не удалось: guide_to(location_id='cafe') — нет локации"
    assert render_action_outcome(failed) == expected


def test_render_action_outcome_ask_visitor_says_question() -> None:
    """stage2 C2: фаза реплики обязана озвучить сам вопрос, а не "выполнено: ...".."""
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="ask_visitor",
        args={
            "question": "Прервать экскурсию и пойти к лидару?",
            "on_yes": {"tool": "guide_to", "args": {"location_id": "livox_mid70"}},
            "on_no": "Хорошо, продолжаем.",
        },
        result_ok=True,
        result_message="",
        result_data={},
    )
    assert render_action_outcome(record) == "задай вопрос: Прервать экскурсию и пойти к лидару?"


# -- read_only-инструменты: полный текст, а не "выполнено: name(...)" ------------


def test_render_action_outcome_read_only_lookup_content_shows_full_text() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="lookup_content",
        args={"content_id": "robo_guide"},
        result_ok=True,
        result_message="",
        result_data={"chunks": ["Раз.", "Два."], "title": "Робот-экскурсовод"},
        read_only=True,
    )
    assert render_action_outcome(record) == "Робот-экскурсовод: Раз. Два."


def test_render_action_outcome_read_only_search_content_shows_hits() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="search_content",
        args={"query": "лидар"},
        result_ok=True,
        result_message="",
        result_data={
            "hits": [
                {
                    "content_id": "livox_mid70",
                    "kind": "exhibit",
                    "title": "Лидар",
                    "text": "Это лидар.",
                }
            ]
        },
        read_only=True,
    )
    assert render_action_outcome(record) == "[exhibit: Лидар] Это лидар."


def test_render_action_outcome_read_only_empty_hits_says_not_found() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="search_content",
        args={"query": "кафе"},
        result_ok=True,
        result_message="",
        result_data={"hits": []},
        read_only=True,
    )
    assert render_action_outcome(record) == "ничего не найдено"


def test_render_action_outcome_read_only_resolve_location_shows_candidates() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="resolve_location",
        args={"query": "лидар"},
        result_ok=True,
        result_message="",
        result_data={"candidates": [{"id": "livox_mid70"}]},
        read_only=True,
    )
    assert render_action_outcome(record) == "возможные локации: livox_mid70"


def test_render_action_outcome_read_only_failure_keeps_short_form() -> None:
    from guide_robot_llm.dialog.turn import ToolCallRecord

    record = ToolCallRecord(
        name="lookup_content",
        args={"content_id": "ghost"},
        result_ok=False,
        result_message="контент не найден",
        result_data={},
        read_only=True,
    )
    assert render_action_outcome(record) == "не удалось: lookup_content — контент не найден"


def test_run_turn_passes_read_only_tools_to_render_action_outcome() -> None:
    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
        del messages, grammar
        call = {
            "tool": "search_content",
            "args": {"query": "x"},
            "confidence": 0.9,
            "abstain": False,
        }
        return CompletionResult(text=json.dumps(call))

    def execute_tool(name: str, args: dict) -> _FakeResult:
        del name, args
        hit = {"kind": "exhibit", "title": "T", "text": "текст факта"}
        return _FakeResult(ok=True, data={"hits": [hit]})

    seen_messages: list[list[dict]] = []

    def complete_answer(messages: list[dict]) -> CompletionResult:
        seen_messages.append(messages)
        return CompletionResult(text="Вот что нашёл.")

    result = _run(
        complete_action=complete_action,
        execute_tool=execute_tool,
        complete_answer=complete_answer,
        tool_names=["reply", "search_content"],
        read_only_tools=frozenset({"search_content"}),
    )

    assert result.action is not None
    assert result.action.read_only is True
    answer_prompt = seen_messages[0][-1]["content"]
    assert "[exhibit: T] текст факта" in answer_prompt
    assert "выполнено: search_content" not in answer_prompt


def test_answer_spoken_as_single_utterance() -> None:
    """Один speak на весь ответ -- без разрыва между предложениями."""
    spoken: list[str] = []

    def complete_answer(messages: list[dict], *, on_delta=None) -> CompletionResult:
        del messages, on_delta
        return CompletionResult(text="Первое предложение. Второе.")

    def speak(text: str) -> _FakeResult:
        spoken.append(text)
        return _FakeResult(ok=True)

    result = _run(complete_answer=complete_answer, speak=speak)
    assert result.say_ok is True
    assert spoken == ["Первое предложение. Второе."]


def test_action_stream_stops_on_complete_action_json() -> None:
    """ADR-0001 §2: раннего stop на `tool=reply` НЕТ -- `abstain` идёт в конце
    объекта, и обрыв раньше него скрал бы сигнал воздержания. Стрим рвётся
    только когда полный JSON становится схема-валидным."""
    full = _reply_call()

    def complete_action(messages, grammar, *, stop_when=None):
        del messages, grammar
        assert stop_when is not None
        assert stop_when('{"tool":"reply","args":') is False
        assert stop_when('{"tool":"reply","args":{},"confidence":0.9,') is False
        assert stop_when(full) is True
        return CompletionResult(text=full, finish_reason="stop_when")

    result = _run(complete_action=complete_action)
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action_raw_text == full


def test_action_stream_stops_on_complete_non_reply_json() -> None:
    def complete_action(messages, grammar, *, stop_when=None):
        del messages, grammar
        assert stop_when is not None
        text = _guide_call()
        assert stop_when(text) is True
        assert stop_when('{"tool":"guide_to","args":{') is False
        return CompletionResult(text=text, finish_reason="stop_when")

    result = _run(complete_action=complete_action)
    assert result.action is not None
    assert result.action.name == "guide_to"


def test_chit_chat_overrides_guide_to_to_reply() -> None:
    """«привет» + guide_to от модели -- не исполняем моторы, уходим в reply."""
    executed: list[str] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append(name)
        return _FakeResult(ok=True)

    result = _run(
        complete_action=_actions(_guide_call()),
        execute_tool=execute_tool,
        utterance="привет",
        complete_answer=_answer("Привет!"),
    )
    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"


def test_start_tour_phrase_overrides_reply() -> None:
    """Живой баг: «начни экскурсию» ушло в reply, тур не стартовал."""
    executed: list[tuple[str, dict]] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append((name, args))
        return _FakeResult(ok=True)

    result = _run(
        complete_action=_actions(_reply_call()),
        execute_tool=execute_tool,
        tool_names=["reply", "start_tour"],
        utterance="начни экскурсию",
        default_tour_id="lab_demo",
        complete_answer=_answer("Начинаем экскурсию."),
    )
    assert executed == [("start_tour", {"tour_id": "lab_demo"})]
    assert result.action is not None
    assert result.action.name == "start_tour"


# -- ADR-0001: strict contract, abstention, safe fallback -------------------


def test_model_abstain_never_executes_any_tool() -> None:
    """abstain=true -- ни один инструмент (включая моторный) не исполняется."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call(confidence=0.9, abstain=True)),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action.think == "abstain_from_model"
    assert result.action_reason_code == "abstain_from_model"
    assert result.stopped_reason == "ok"
    assert result.repair_used is False


def test_low_confidence_motor_action_cannot_execute() -> None:
    """Ключевой тест ишью #5: guide_to с confidence ниже порога не доезжает до
    execute_tool -- safe abstention ДО брокера."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call(confidence=0.3)),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        confidence_threshold=0.5,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action.think == "low_confidence"
    assert result.action_reason_code == "low_confidence"
    assert result.stopped_reason == "ok"


def test_confidence_equal_to_threshold_is_allowed() -> None:
    """Порог включительный: confidence == threshold -- действие исполняется."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call(confidence=0.5)),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        confidence_threshold=0.5,
    )

    assert executed == ["guide_to"]
    assert result.action_reason_code == ""


def test_confidence_above_threshold_is_not_trust() -> None:
    """confidence не авторизует: чужой id отклоняется даже при confidence=1.0."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_guide_call(location_id="ghost", confidence=1.0)),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        known_location_ids=frozenset({"cafe"}),
        repair_attempts=0,
    )

    assert executed == []
    assert result.action_reason_code == "unknown_id"
    assert result.action is not None
    assert result.action.name == "reply"


def test_unknown_id_goes_to_repair_then_fallback() -> None:
    """Чужой id: сначала repair-попытка, затем действие исполняется."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(
            _guide_call(location_id="ghost"),
            _guide_call(location_id="cafe"),
        ),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        known_location_ids=frozenset({"cafe"}),
        repair_attempts=1,
    )

    assert executed == ["guide_to"]
    assert result.repair_used is True
    assert result.action_reason_code == ""


def test_illegal_state_tool_goes_to_repair_then_fallback() -> None:
    """Инструмент вне tools_allowed: repair, затем safe fallback, брокер не видит."""
    executed: list[str] = []

    def complete_action(messages: list[dict], grammar: str, **_kwargs) -> CompletionResult:
        del messages, grammar
        return CompletionResult(
            text=json.dumps(
                {
                    "tool": "start_tour",
                    "args": {"tour_id": "lab_demo"},
                    "confidence": 0.9,
                    "abstain": False,
                }
            )
        )

    result = _run(
        complete_action=complete_action,
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=["reply"],  # start_tour недоступен в этом состоянии
        repair_attempts=1,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action.think == "illegal_state"
    assert result.action_reason_code == "illegal_state"
    assert result.stopped_reason == "ok"
    assert result.repair_used is True


def test_malformed_action_goes_to_repair_then_fallback() -> None:
    """Malformed (чужой ключ) -> repair -> снова malformed -> action_parse_error."""
    executed: list[str] = []
    malformed = (
        '{"tool": "reply", "args": {}, "confidence": 0.9, '
        '"abstain": false, "think": "лишнее поле"}'
    )

    result = _run(
        complete_action=_actions(malformed, malformed),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        repair_attempts=1,
    )

    assert executed == []
    assert result.action is None
    assert result.stopped_reason == "action_parse_error"
    assert result.repair_used is True


def test_action_first_attempt_valid_metric() -> None:
    """Метрика #11: first-attempt schema validity в обе стороны."""
    ok_result = _run(complete_action=_actions(_guide_call()))
    assert ok_result.action_first_attempt_valid is True

    fixed = _run(
        complete_action=_actions('not json at all', _guide_call()),
        repair_attempts=1,
    )
    assert fixed.action_first_attempt_valid is False


def test_safe_fallback_answer_phase_is_told_the_reason() -> None:
    """При abstention фаза реплики получает инструкцию прояснить/сказать о
    неопределённости, а не описывать несуществующее действие."""
    answer_messages: list[dict] = []

    def complete_answer(
        messages: list[dict], grammar: str | None = None, **_kwargs
    ) -> CompletionResult:
        answer_messages.extend(messages)
        return CompletionResult(text="уточняющий ответ")

    _run(
        complete_action=_actions(_guide_call(confidence=0.3)),
        complete_answer=complete_answer,
    )

    joined = "\n".join(str(m.get("content", "")) for m in answer_messages)
    assert "low_confidence" in joined
    assert "уточняющим" in joined
    assert "НЕ было исполнено" in joined


def test_override_start_tour_bypasses_verdict() -> None:
    """host-override стартует тур даже по low-confidence reply (override =
    намерение посетителя, а не доверие к модели)."""
    executed: list[tuple[str, dict]] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append((name, args))
        return _FakeResult(ok=True)

    result = _run(
        complete_action=_actions(_reply_call(confidence=0.2)),
        execute_tool=execute_tool,
        tool_names=["reply", "start_tour"],
        utterance="начни экскурсию",
        default_tour_id="lab_demo",
        complete_answer=_answer("Начинаем."),
    )

    assert executed == [("start_tour", {"tour_id": "lab_demo"})]
    assert result.action is not None
    assert result.action.name == "start_tour"
    assert result.action_reason_code == ""


# ---------------------------------------------------------------------------
# Таига #7: resolve_pointing -- host-side геометрия ДО execute_tool
# ---------------------------------------------------------------------------


def test_resolve_pointing_resolved_executes_tool() -> None:
    """Уникальный видимый кандидат + жест на нём + выбор модели -- исполняется."""
    executed: list[tuple[str, dict]] = []

    def execute_tool(name: str, args: dict) -> _FakeResult:
        executed.append((name, args))
        return _FakeResult(ok=True, data={"chunks": ["текст"]})

    result = _run(
        complete_action=_actions(_pt_call("robot")),
        execute_tool=execute_tool,
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robot"}),
        pointing_context=_pt_ctx_resolved(),
    )

    assert executed == [("resolve_pointing", {"content_id": "robot"})]
    assert result.action is not None
    assert result.action.name == "resolve_pointing"
    assert result.action_reason_code == ""


def test_resolve_pointing_no_context_abstains_stale() -> None:
    """Нет замороженного контекста (кадров нет) -- не исполняем, stale_frames."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_pt_call("robot")),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robot"}),
        pointing_context=None,
        repair_attempts=0,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action.think == REASON_STALE_FRAMES
    assert result.action_reason_code == REASON_STALE_FRAMES


def test_resolve_pointing_ambiguous_abstains() -> None:
    """Два похожих рядом -- неоднозначно, уточняем, не угадываем."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_pt_call("left")),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"left", "right"}),
        pointing_context=_pt_ctx_ambiguous(),
        repair_attempts=0,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action.think == REASON_AMBIGUOUS_TARGET
    assert result.action_reason_code == REASON_AMBIGUOUS_TARGET


def test_resolve_pointing_stale_frames_abstains() -> None:
    """Кадры устарели -- качество ввода, stale_frames, не исполняем."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_pt_call("robot")),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robot"}),
        pointing_context=_pt_ctx_stale(),
        repair_attempts=0,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.think == REASON_STALE_FRAMES
    assert result.action_reason_code == REASON_STALE_FRAMES


def test_resolve_pointing_no_candidate_abstains() -> None:
    """Нет правдоподобных видимых кандидатов -- no_candidate, уточняем."""
    executed: list[str] = []

    result = _run(
        complete_action=_actions(_pt_call("far")),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"far"}),
        pointing_context=_pt_ctx_no_candidate(),
        repair_attempts=0,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.think == REASON_NO_CANDIDATE
    assert result.action_reason_code == REASON_NO_CANDIDATE


def test_resolve_pointing_unknown_id_never_reaches_execute() -> None:
    """content_id вне каталога -- unknown_id ДО брокера, execute_tool не зовётся
    ни разу (даже при попытке починки модель снова шлёт чужой id)."""
    executed: list[str] = []

    # Модель упрямо шлёт выдуманный id на каждой попытке.
    result = _run(
        complete_action=_actions(_pt_call("ghost"), _pt_call("ghost")),
        execute_tool=lambda name, args: executed.append(name) or _FakeResult(ok=True),
        tool_names=[*_TOOL_NAMES, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robot"}),
        pointing_context=_pt_ctx_resolved(),
        repair_attempts=1,
    )

    assert executed == []
    assert result.action is not None
    assert result.action.name == "reply"
    assert result.action_reason_code == "unknown_id"
