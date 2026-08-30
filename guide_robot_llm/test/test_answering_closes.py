"""Регресс на главный исправленный баг: в ANSWERING ход обязан закрыть кадр.

DIALOG_REWORK_PLAN.md §9.3: раньше `say` был доступен ЛЛМ как действие
фазы выбора, и модель могла раз за разом отвечать `say` в `ANSWERING`,
никогда не вызывая `finish_answer` -- кадр не закрывался, FSM висел до
`answer_max_s`. Теперь `say` не входит в каталог, видимый модели
(`schema.allowed_tools(state, llm_only=True)`), поэтому в `ANSWERING`
единственные действия фазы 2 -- `finish_answer`/`stop_tour`/`reply`. Этот
тест фиксирует инвариант на уровне каталога и на уровне полного хода
(`run_turn` на фейках), без ROS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from guide_robot_llm.dialog.turn import run_turn
from guide_robot_llm.llm_client import CompletionResult
from guide_robot_llm.tools.schema import allowed_tools

from guide_robot_msgs.msg import MissionState


@dataclass
class _FakeResult:
    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


def test_say_not_in_llm_visible_catalog_for_answering() -> None:
    visible = allowed_tools(MissionState.STATE_ANSWERING, llm_only=True)
    assert "say" not in visible
    assert "finish_answer" in visible
    assert "reply" in visible


def test_turn_in_answering_ends_via_finish_answer_or_reply_never_say() -> None:
    tool_names = allowed_tools(MissionState.STATE_ANSWERING, llm_only=True)

    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        return CompletionResult(text="Это макет университетского кампуса.")

    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        return CompletionResult(
            text=json.dumps({"tool": "finish_answer", "args": {"outcome": 0}})
        )

    result = run_turn(
        system_prompt="sys",
        history_messages=[],
        user_content="{}",
        complete_answer=complete_answer,
        complete_action=complete_action,
        speak=lambda text: _FakeResult(ok=True),
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=tool_names,
        action_instruction="action instruction",
        answer_instruction="answer instruction",
    )

    assert result.action is not None
    assert result.action.name in ("finish_answer", "reply")
    assert result.stopped_reason == "ok"


def test_turn_in_answering_reply_still_closes_the_turn_not_the_frame() -> None:
    """`reply` -- полноправное действие: ход заканчивается, даже если кадр `ANSWERING`
    не закрыт этим ходом -- закрытие кадра остаётся отдельным решением модели/FSM,
    не блокирующим завершение ХОДА диалога."""
    tool_names = allowed_tools(MissionState.STATE_ANSWERING, llm_only=True)

    def complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        return CompletionResult(text="Секунду, ещё расскажу подробнее.")

    def complete_action(messages: list[dict], grammar: str) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=json.dumps({"tool": "reply", "args": {}}))

    result = run_turn(
        system_prompt="sys",
        history_messages=[],
        user_content="{}",
        complete_answer=complete_answer,
        complete_action=complete_action,
        speak=lambda text: _FakeResult(ok=True),
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=tool_names,
        action_instruction="action instruction",
        answer_instruction="answer instruction",
    )

    assert result.stopped_reason == "ok"
    assert result.action.name == "reply"
