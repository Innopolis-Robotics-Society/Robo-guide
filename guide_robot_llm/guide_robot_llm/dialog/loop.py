"""ReAct-шаг dialog_agent: сообщения -> tool call -> результат -> сообщения (llm_plam.md §3/§5).

Чистая логика без rclpy -- backend и исполнитель инструмента инжектируются
как обычные callable, тестируется на фейках без ROS и без HTTP
(`dialog_agent_node.py` -- единственный потребитель из rclpy-контекста).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from guide_robot_llm.llm_client import CompletionResult, build_tool_call_grammar
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError

__all__ = ["ReactTurnResult", "ToolCallRecord", "ToolResultLike", "run_react_turn"]

# Инструменты, после которых ход диалога закончен -- дальнейший tool-call в
# ЭТОМ же ходе не нужен (say/tell_about -- реплика сказана, confirm/
# finish_answer/stop_tour/start_tour/guide_to/tour_by_points/pause/resume --
# решение принято и уйдёт в ROS отдельным действием/сервисом). Read-only
# справочники (list_*/estimate_route) НЕ терминальны -- модель обычно зовёт
# их, чтобы узнать данные для следующего шага, а не как финальный ответ.
_TERMINAL_TOOLS = frozenset(
    {
        "say",
        "tell_about",
        "confirm",
        "finish_answer",
        "stop_tour",
        "start_tour",
        "guide_to",
        "tour_by_points",
        "pause",
        "resume",
    }
)


class ToolResultLike(Protocol):
    """Поля результата вызова инструмента (см. `tool_broker_node.ToolResult`)."""

    ok: bool
    message: str
    data: dict


@dataclass
class ToolCallRecord:
    """Один выполненный за ход tool-call и его исход."""

    name: str
    args: dict
    result_ok: bool
    result_message: str
    result_data: dict


@dataclass
class ReactTurnResult:
    """Итог одного хода: полный лог сообщений + все вызовы + причина остановки."""

    messages: list[dict]
    calls: list[ToolCallRecord] = field(default_factory=list)
    # "terminal_tool" | "max_calls" | "parse_error" | "backend_error"
    stopped_reason: str = "max_calls"


def run_react_turn(
    *,
    system_prompt: str,
    user_content: str,
    complete: Callable[[list[dict], str], CompletionResult],
    execute_tool: Callable[[str, dict], ToolResultLike],
    tool_names: Sequence[str],
    max_tool_calls: int = 2,
) -> ReactTurnResult:
    """Прогнать один ход: до `max_tool_calls` итераций `complete()` -> `execute_tool()`.

    `complete(messages, grammar)` уже связан вызывающим (`dialog_agent_node.py`)
    с конкретными бэкендами/`abort_event`/ретраями -- здесь про это ничего не
    известно и не должно быть известно. `BackendAborted` (barge-in) НЕ
    перехватывается -- пробрасывается наружу как есть: вызывающий поток
    обязан отличить намеренное прерывание от реального отказа бэкенда
    (разное логирование/метрики, разное решение "ретраить ли"). Остальные
    `BackendError` (timeout/HTTP/сеть) перехватываются -- оборванный ход
    здесь ожидаемый штатный случай (llm_plam.md §6: "деградация корректная"),
    не повод поднимать исключение вызывающему.

    GBNF гарантирует синтаксическую валидность JSON на выходе модели -- если
    `json.loads` всё равно падает или форма не та (`tool`/`args` не то, что
    ожидалось), это баг сервера/грамматики, перехватывается как
    `parse_error`, не поднимается дальше.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    calls: list[ToolCallRecord] = []
    grammar = build_tool_call_grammar(tool_names)

    for _ in range(max_tool_calls):
        try:
            completion = complete(messages, grammar)
        except BackendAborted:
            raise
        except BackendError:
            return ReactTurnResult(messages=messages, calls=calls, stopped_reason="backend_error")

        try:
            parsed = json.loads(completion.text.strip())
            name = parsed["tool"]
            args = parsed.get("args", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                msg = "неверная форма tool-call JSON"
                raise ValueError(msg)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return ReactTurnResult(messages=messages, calls=calls, stopped_reason="parse_error")

        messages.append({"role": "assistant", "content": completion.text})

        result = execute_tool(name, args)
        calls.append(
            ToolCallRecord(
                name=name,
                args=args,
                result_ok=result.ok,
                result_message=result.message,
                result_data=dict(result.data),
            )
        )
        result_payload = json.dumps(
            {"ok": result.ok, "message": result.message, "data": result.data}
        )
        messages.append({"role": "user", "content": result_payload})

        if name in _TERMINAL_TOOLS:
            return ReactTurnResult(messages=messages, calls=calls, stopped_reason="terminal_tool")

    return ReactTurnResult(messages=messages, calls=calls, stopped_reason="max_calls")
