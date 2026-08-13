"""Ход dialog_agent: действие (фаза 1, GBNF+think) -> исполнение -> реплика (фаза 2).

Порядок фаз ИНВЕРТИРОВАН против DIALOG_REWORK_PLAN.md §0/§5 (живой баг:
реплика «отвожу вас к кафе» + действие noop в том же ходу): сначала модель
под GBNF выбирает действие -- `{"think": ..., "tool": ..., "args": ...}`,
где `think` -- короткое явное рассуждение (ReAct-Thought), инструмент
исполняется через `tool_broker`, и только потом генерируется свободная
реплика, которая видит think, действие и его РЕАЛЬНЫЙ итог -- согласованность
речи с действием структурная, а не «просьба в промпте». Цена -- первая речь
начинается позже на длительность фазы действия (temp 0, короткий JSON);
взамен посетитель слышит правду («веду вас к входу» / «не получилось»),
а починка провалившегося вызова происходит ДО того, как что-то сказано.

Чистая логика без rclpy -- backend'ы, `speak`/`execute_tool` инжектируются
как callable, тестируется на фейках без ROS и без HTTP
(`dialog_agent_node.py` -- единственный потребитель из rclpy-контекста).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from guide_robot_llm.dialog.sanitize import sanitize_answer
from guide_robot_llm.llm_client import CompletionResult, build_tool_call_grammar
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError

__all__ = ["ToolCallRecord", "ToolResultLike", "TurnResult", "render_action_outcome", "run_turn"]


class ToolResultLike(Protocol):
    """Поля результата вызова инструмента (см. `tool_broker_node.ToolResult`)."""

    ok: bool
    message: str
    data: dict


@dataclass
class ToolCallRecord:
    """Действие, выбранное фазой действия, и его исход (включая `noop`)."""

    name: str
    args: dict
    result_ok: bool
    result_message: str
    result_data: dict
    think: str = ""


@dataclass
class TurnResult:
    """Итог одного хода «действие -> реплика».

    `messages` -- ПОЛНЫЙ накопленный диалог с ЛЛМ на момент окончания хода:
    system prompt, история, реплика посетителя, сырой tool-call текст на
    каждой попытке починки, инструкция и текст фазы реплики. Единственный
    источник, где виден буквально весь обмен с моделью за ход
    (`interaction_log` кладёт его в запись как есть).

    `answer_raw_text`/`action_raw_text` -- то, что модель выдала ДО
    постобработки (`sanitize_answer` для реплики, `json.loads` для действия).
    `action_raw_text` -- текст ПОСЛЕДНЕЙ попытки фазы действия (включая
    случай `action_parse_error`, когда `action` остаётся `None`, но модель
    что-то всё-таки прислала). `*_finish_reason` -- как сервер объяснил
    остановку генерации, пусто, если бэкенд не ответил вовсе.

    Семантика `stopped_reason` при инвертированном порядке фаз:
    `action_backend_error` -- бэкенд упал на ПЕРВОЙ фазе, не произошло
    вообще ничего (ни действия, ни речи); `answer_backend_error` -- действие
    могло уже ИСПОЛНИТЬСЯ, но реплика не сгенерировалась и ничего не
    сказано; `action_parse_error` -- баг сервера/грамматики, ход обрывается
    до исполнения и до речи; `action_invalid` -- инструмент исполнён с
    ошибкой и починка исчерпана (реплика при этом генерируется и честно
    сообщает о провале); `aborted` -- barge-in/новая реплика посетителя.
    """

    messages: list[dict] = field(default_factory=list)
    answer_text: str = ""
    answer_raw_text: str = ""
    answer_finish_reason: str = ""
    action_raw_text: str = ""
    action_finish_reason: str = ""
    say_ok: bool = False
    action: ToolCallRecord | None = None
    repair_used: bool = False
    # "ok" | "answer_backend_error" | "action_backend_error"
    # | "action_parse_error" | "action_invalid" | "aborted"
    stopped_reason: str = "ok"


def render_action_outcome(record: ToolCallRecord | None) -> str:
    """Отрендерить итог действия одной строкой -- для промпта фазы реплики.

    Та же строка (для не-noop) используется `dialog_agent_node._action_event_text`
    как событие истории: то, на что опиралась реплика, и то, что запомнит
    история, обязаны совпадать побайтово.
    """
    if record is None or record.name == "noop":
        return "noop (никакого действия не выполнялось)"
    args_str = ", ".join(f"{key}={value!r}" for key, value in record.args.items())
    call = f"{record.name}({args_str})"
    if record.result_ok:
        return f"выполнено: {call}"
    return f"не удалось: {call} — {record.result_message}"


def run_turn(
    *,
    system_prompt: str,
    history_messages: list[dict],
    user_content: str,
    complete_answer: Callable[[list[dict]], CompletionResult],
    complete_action: Callable[[list[dict], str], CompletionResult],
    speak: Callable[[str], ToolResultLike],
    execute_tool: Callable[[str, dict], ToolResultLike],
    tool_names: Sequence[str],
    action_instruction: str,
    answer_instruction: str,
    repair_attempts: int = 1,
    check_aborted: Callable[[], bool] = lambda: False,
    answer_max_chars: int = 400,
) -> TurnResult:
    """Прогнать один ход: действие (GBNF) -> исполнение -> реплика -> `speak()`.

    `complete_answer`/`complete_action` уже связаны вызывающим
    (`dialog_agent_node.py`) с конкретными бэкендами/`abort_event`/
    max_tokens/temperature/грамматикой (только `complete_action` идёт под
    грамматикой). `BackendAborted` (barge-in) НЕ перехватывается ни в одной
    из фаз -- пробрасывается наружу как есть, вызывающий поток обязан
    отличить намеренное прерывание от реального отказа бэкенда. Остальные
    `BackendError` (timeout/HTTP/сеть) -- оборванный ход здесь штатный
    случай деградации, не повод поднимать исключение вызывающему.

    `check_aborted()` зовётся в двух точках: перед `execute_tool()` (barge-in
    не должен исполнить уже неактуальное действие) и перед `speak()` (если
    посетитель успел отменить до того, как робот начал говорить, начинать
    нельзя) -- в обоих случаях ход останавливается со
    `stopped_reason="aborted"`.

    Починка (`repair_attempts`, по умолчанию 1) срабатывает только на
    ПРОВАЛИВШЕМСЯ исполнении (`execute_tool().ok is False`) -- не на
    `action_parse_error`: если модель прислала не тот JSON, синтаксическая
    починка средствами этого модуля бессмысленна (GBNF и так гарантирует
    форму, а провал парсинга -- баг сервера/грамматики). Починка происходит
    ДО фазы реплики -- посетитель её не слышит и не замечает.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_content},
        {"role": "user", "content": action_instruction},
    ]

    grammar = build_tool_call_grammar(tool_names)
    repair_used = False
    attempts_left = repair_attempts
    record: ToolCallRecord | None = None
    action_raw_text = ""
    action_finish_reason = ""

    while True:
        try:
            action_completion = complete_action(messages, grammar)
        except BackendAborted:
            raise
        except BackendError:
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                stopped_reason="action_backend_error",
            )

        action_raw_text = action_completion.text
        action_finish_reason = action_completion.finish_reason
        # Сырой текст попадает в messages ДО проверки парсинга -- иначе
        # `action_parse_error` (модель прислала не ту форму) теряет
        # единственную улику, что она вообще ответила, и чем именно.
        messages = [*messages, {"role": "assistant", "content": action_raw_text}]

        parsed = _parse_tool_call(action_raw_text)
        if parsed is None:
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                stopped_reason="action_parse_error",
            )
        think, name, args = parsed

        if name == "noop":
            record = ToolCallRecord(
                name=name, args=args, result_ok=True, result_message="", result_data={},
                think=think,
            )
            break

        if check_aborted():
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                stopped_reason="aborted",
            )

        result = execute_tool(name, args)
        record = ToolCallRecord(
            name=name,
            args=args,
            result_ok=result.ok,
            result_message=result.message,
            result_data=dict(result.data),
            think=think,
        )

        if result.ok or attempts_left <= 0:
            break

        attempts_left -= 1
        repair_used = True
        messages = [*messages, {"role": "user", "content": result.message}]

    action_stopped_reason = "ok" if record.result_ok else "action_invalid"

    # Фаза реплики: статичная инструкция ПЕРВОЙ, волатильный итог действия --
    # хвостом (CACHE_REUSE: префикс до итога совпадает от хода к ходу).
    outcome_line = render_action_outcome(record)
    messages = [
        *messages,
        {"role": "user", "content": f"{answer_instruction}\n\nИтог действия: {outcome_line}"},
    ]

    try:
        answer_completion = complete_answer(messages)
    except BackendAborted:
        raise
    except BackendError:
        return TurnResult(
            messages=messages,
            action_raw_text=action_raw_text,
            action_finish_reason=action_finish_reason,
            action=record,
            repair_used=repair_used,
            stopped_reason="answer_backend_error",
        )

    answer_raw_text = answer_completion.text
    answer_finish_reason = answer_completion.finish_reason
    answer_text = sanitize_answer(answer_raw_text, max_chars=answer_max_chars)
    messages = [*messages, {"role": "assistant", "content": answer_raw_text}]

    say_ok = False
    if answer_text:
        if check_aborted():
            return TurnResult(
                messages=messages,
                answer_text=answer_text,
                answer_raw_text=answer_raw_text,
                answer_finish_reason=answer_finish_reason,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                action=record,
                repair_used=repair_used,
                stopped_reason="aborted",
            )
        say_ok = speak(answer_text).ok

    return TurnResult(
        messages=messages,
        answer_text=answer_text,
        answer_raw_text=answer_raw_text,
        answer_finish_reason=answer_finish_reason,
        action_raw_text=action_raw_text,
        action_finish_reason=action_finish_reason,
        say_ok=say_ok,
        action=record,
        repair_used=repair_used,
        stopped_reason=action_stopped_reason,
    )


def _parse_tool_call(raw_text: str) -> tuple[str, str, dict] | None:
    """Разобрать `{"think": "...", "tool": "...", "args": {...}}`.

    `None` -- форма невалидна (баг сервера/GBNF). Отсутствующий или
    не-строковый `think` толерантно превращается в пустую строку -- это
    диагностическое поле, его порча не повод терять действие.
    """
    try:
        parsed = json.loads(raw_text.strip())
        name = parsed["tool"]
        args = parsed.get("args", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            msg = "неверная форма tool-call JSON"
            raise ValueError(msg)
        think = parsed.get("think", "")
        if not isinstance(think, str):
            think = ""
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
        return None
    return think, name, args
