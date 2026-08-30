#!/usr/bin/env python3
"""Прогнать golden-набор против ЖИВОГО `llm_server`, напечатать метрики "до/после".

DIALOG_REWORK_PLAN.md §9.2. НЕ тест в CI -- нужен реально поднятый
`llm_server/` (см. `../llm_server/README.md`), скрипт для ручного прогона.
Печатает: accuracy выбора инструмента (+матрица ошибок по состояниям), долю
ходов с непустым ответом фазы 1, долю ходов с цитированием (`verbatim_overlap
_words >= 8`), p50/p95 по `llm_answer`/`llm_action` раздельно, долю `noop` в
`ANSWERING` (предвестник таймаута `answer_max_s`, если кадр не закрывается).

```
python3 scripts/eval_turns.py --base-url http://127.0.0.1:18080/v1
```
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guide_robot_llm.dialog.prompt import (  # noqa: E402
    build_action_instruction,
    build_answer_instruction,
    build_system_prompt,
)
from guide_robot_llm.dialog.turn import TurnResult, run_turn  # noqa: E402
from guide_robot_llm.dialog.verbatim import max_shingle_overlap  # noqa: E402
from guide_robot_llm.llm_client import Backend, BackendConfig, complete_with_fallback  # noqa: E402
from guide_robot_llm.snapshot import render_status_line  # noqa: E402
from guide_robot_llm.tools import schema  # noqa: E402

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GOLDEN = _PACKAGE_ROOT / "test" / "data" / "turns_golden.jsonl"
_DEFAULT_PREAMBLE = _PACKAGE_ROOT / "config" / "system_prompt.txt"
_VERBATIM_FLAG_WORDS = 8


@dataclass
class _FakeToolResult:
    """Действие фазы 2 здесь НЕ исполняется по-настоящему -- нужен только выбор модели.

    `result.action.name` читается вызывающим, реального эффекта в mission_control нет.
    """

    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class _Outcome:
    result: TurnResult
    mission_state: str
    expected_tool: str | None
    verbatim_overlap: int
    llm_answer_ms: float | None
    llm_action_ms: float | None


def _load_jsonl(path: Path) -> list[dict]:
    records = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _run_one(
    backend: Backend,
    system_prompt: str,
    action_instruction: str,
    answer_instruction: str,
    corpus_texts: list[str],
    record: dict,
) -> _Outcome:
    snapshot = record["snapshot"]
    utterance = record["utterance"]
    mission_state = snapshot.get("mission", {}).get("state", "UNKNOWN")
    tools_allowed = snapshot.get("tools_allowed", [])

    # Тот же формат, что `dialog_agent_node._run_turn`: служебная строка
    # состояния + реплика последней строкой (голый текст, не JSON).
    user_content = "\n".join([render_status_line(snapshot), utterance])

    timings: dict[str, float] = {}

    def complete_answer(messages: list[dict]):
        start = time.monotonic()
        try:
            return complete_with_fallback(
                [backend],
                messages,
                grammar=None,
                max_tokens=160,
                temperature=0.6,
                # stage5 п.3: тот же default, что llm.answer_frequency_penalty
                # в config/llm.yaml -- иначе golden-прогон не проверяет фикс.
                frequency_penalty=0.4,
            )
        finally:
            timings["llm_answer"] = (time.monotonic() - start) * 1000

    def complete_action(messages: list[dict], grammar: str):
        start = time.monotonic()
        try:
            return complete_with_fallback(
                [backend], messages, grammar=grammar, max_tokens=128, temperature=0.0
            )
        finally:
            timings["llm_action"] = (time.monotonic() - start) * 1000

    result = run_turn(
        system_prompt=system_prompt,
        history_messages=[],
        user_content=user_content,
        complete_answer=complete_answer,
        complete_action=complete_action,
        speak=lambda text: _FakeToolResult(ok=True),
        execute_tool=lambda name, args: _FakeToolResult(ok=True),
        tool_names=tools_allowed,
        action_instruction=action_instruction,
        answer_instruction=answer_instruction,
        # stage5 п.2: без якоря golden-прогон не проверяет фикс "отвечает на
        # позапрошлый вопрос" -- utterance обязана дойти до фазы реплики.
        utterance=utterance,
    )
    overlap = max_shingle_overlap(result.answer_text, corpus_texts)
    return _Outcome(
        result=result,
        mission_state=mission_state,
        expected_tool=record.get("expected_tool"),
        verbatim_overlap=overlap,
        llm_answer_ms=timings.get("llm_answer"),
        llm_action_ms=timings.get("llm_action"),
    )


def _args_match(expected: dict, actual: dict | None) -> bool:
    """`expected` -- подмножество `actual`, а не точное равенство: golden-запись
    заявляет только то, что ей важно (например `outcome`), не весь набор args."""
    if actual is None:
        return False
    return all(actual.get(key) == value for key, value in expected.items())


def _answer_contains_any(answer_text: str, expected_substrings: list[str]) -> bool:
    return any(substring in answer_text for substring in expected_substrings)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def evaluate(
    golden_path: Path, preamble_path: Path, base_url: str, dump_jsonl: Path | None = None
) -> None:
    """Прогнать `golden_path` через `base_url`, напечатать сводку метрик в stdout.

    Локальный корпус знаний убран (CLAUDE_CODE_TASK_stage1_knowledge.md
    п.5): `verbatim_overlap_words` здесь больше не считается против всего
    корпуса, только `""` -- полная адаптация под справку из
    `guide_robot_semantic_map` (per-ход `references`) -- отдельная задача
    (п.9.1), не входит в этот скрипт-заготовку.

    `dump_jsonl` (stage3.5 §5) -- один JSON-объект на кейс golden-набора
    (utterance/mission_state/expected_tool/actual_tool/args/answer_text/
    pass), для приложения "до"/"после" правок промпта как двух файлов.

    Опциональные поля golden-записи (stage5 п.5): `expected_args` (dict) --
    сверяется как подмножество `args` реального вызова, ТОЛЬКО если
    инструмент уже совпал (иначе двойной провал считается одной ошибкой
    выбора инструмента, не двумя); `expected_answer_contains` (list[str]) --
    `answer_text` обязан содержать хотя бы одну из подстрок (случай "четыре"
    ИЛИ "4" для арифметики -- единственный кейс с проверкой текста, она
    детерминирована достаточно, чтобы не флакать на свободном тексте).
    """
    records = _load_jsonl(golden_path)
    preamble = preamble_path.read_text(encoding="utf-8")
    corpus_texts: list[str] = []
    system_prompt = build_system_prompt(preamble)
    action_instruction = build_action_instruction(schema.TOOLS)
    answer_instruction = build_answer_instruction()
    backend = Backend(BackendConfig(base_url=base_url, read_timeout_s=60.0))

    outcomes = [
        _run_one(
            backend, system_prompt, action_instruction, answer_instruction, corpus_texts, record
        )
        for record in records
    ]
    total = len(outcomes)
    if total == 0:
        print("golden-набор пуст -- нечего оценивать", file=sys.stderr)
        return

    tool_correct = 0
    confusion: Counter[tuple[str, str, str]] = Counter()
    content_failures: list[str] = []
    non_empty_answers = 0
    verbatim_flagged = 0
    answering_reply = 0
    answering_total = 0
    answer_times: list[float] = []
    action_times: list[float] = []
    dump_records: list[dict] = []

    for outcome, record in zip(outcomes, records, strict=True):
        actual_tool = outcome.result.action.name if outcome.result.action is not None else None
        actual_args = outcome.result.action.args if outcome.result.action is not None else None
        tool_ok = actual_tool == outcome.expected_tool
        passed = tool_ok
        if tool_ok:
            expected_args = record.get("expected_args")
            if expected_args and not _args_match(expected_args, actual_args):
                passed = False
                content_failures.append(
                    f"{record['utterance']!r}: args {actual_args} != ожидали {expected_args}"
                )
            expected_answer_contains = record.get("expected_answer_contains")
            if expected_answer_contains and not _answer_contains_any(
                outcome.result.answer_text, expected_answer_contains
            ):
                passed = False
                content_failures.append(
                    f"{record['utterance']!r}: ответ {outcome.result.answer_text!r} "
                    f"не содержит ни одной из {expected_answer_contains}"
                )
        if passed:
            tool_correct += 1
        elif not tool_ok:
            confusion[(outcome.mission_state, str(outcome.expected_tool), str(actual_tool))] += 1

        if outcome.result.answer_text.strip():
            non_empty_answers += 1
        if outcome.verbatim_overlap >= _VERBATIM_FLAG_WORDS:
            verbatim_flagged += 1

        if outcome.mission_state == "ANSWERING":
            answering_total += 1
            if actual_tool == "reply":
                answering_reply += 1

        if outcome.llm_answer_ms is not None:
            answer_times.append(outcome.llm_answer_ms)
        if outcome.llm_action_ms is not None:
            action_times.append(outcome.llm_action_ms)

        dump_records.append(
            {
                "utterance": record["utterance"],
                "mission_state": outcome.mission_state,
                "expected_tool": outcome.expected_tool,
                "actual_tool": actual_tool,
                "args": actual_args,
                "answer_text": outcome.result.answer_text,
                "pass": passed,
            }
        )

    print(f"ходов: {total}")
    print(f"accuracy выбора инструмента: {tool_correct}/{total} = {tool_correct / total:.1%}")
    if confusion:
        print("ошибки (mission_state: ожидали -> получили): count")
        for (state, expected, actual), count in sorted(confusion.items(), key=lambda kv: -kv[1]):
            print(f"  {state}: {expected} -> {actual}: {count}")
    if content_failures:
        print("инструмент выбран верно, но args/текст ответа не совпали:")
        for note in content_failures:
            print(f"  {note}")
    print(
        f"доля ходов с непустым ответом фазы 1: "
        f"{non_empty_answers}/{total} = {non_empty_answers / total:.1%}"
    )
    print(
        f"доля ходов с цитированием (overlap >= {_VERBATIM_FLAG_WORDS} слов): "
        f"{verbatim_flagged}/{total} = {verbatim_flagged / total:.1%}"
    )
    if answering_total:
        print(
            f"доля reply в ANSWERING: "
            f"{answering_reply}/{answering_total} = {answering_reply / answering_total:.1%}"
        )
    if answer_times:
        print(
            f"llm_answer p50={_percentile(answer_times, 0.5):.0f}ms "
            f"p95={_percentile(answer_times, 0.95):.0f}ms"
        )
    if action_times:
        print(
            f"llm_action p50={_percentile(action_times, 0.5):.0f}ms "
            f"p95={_percentile(action_times, 0.95):.0f}ms"
        )

    if dump_jsonl is not None:
        with dump_jsonl.open("w", encoding="utf-8") as f:
            for dump_record in dump_records:
                f.write(json.dumps(dump_record, ensure_ascii=False) + "\n")
        print(f"дамп по кейсам: {dump_jsonl}")


def main() -> None:
    """CLI-обвязка."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=_DEFAULT_GOLDEN)
    parser.add_argument("--preamble", type=Path, default=_DEFAULT_PREAMBLE)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--dump-jsonl", type=Path, default=None)
    args = parser.parse_args()
    evaluate(args.golden, args.preamble, args.base_url, dump_jsonl=args.dump_jsonl)


if __name__ == "__main__":
    main()
