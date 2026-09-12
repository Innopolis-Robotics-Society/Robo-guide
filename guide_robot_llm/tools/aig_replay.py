#!/usr/bin/env python3
"""Прогнать синтетические ходы robo-guide через AI Router: reasoning, задержка, выбор инструмента.

`synthetic_turns.jsonl` собран реальным кодом guide_robot_llm (build_system_prompt,
build_action_instruction, run_turn, DialogHistory, render_status_line): для каждого хода
лежат готовые запросы фазы действия и фазы реплики. Каталог локаций и СПРАВКА синтетические.

    read -rs AIG_KEY && export AIG_KEY
    python3 aig_replay.py --variants plain            # как сейчас: две фазы
    python3 aig_replay.py --turns synthetic_turns_inline.jsonl        # reply сразу в args.text
    python3 aig_replay.py --turns synthetic_turns_inline.jsonl --gate json_schema  # + enum
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

VARIANTS: dict[str, dict] = {
    "plain": {},
    "effort_none": {"reasoning": {"effort": "none"}},
    "effort_low": {"reasoning": {"effort": "low"}},
}


def stream_once(base: str, key: str, payload: dict) -> dict:
    """Отправить один стримящийся запрос, собрать текст/тайминги/usage/warnings."""
    t0 = time.monotonic()
    t_content = None
    content: list[str] = []
    usage: dict = {}
    warnings: list[str] = []
    finish = ""
    with requests.post(
        f"{base.rstrip('/')}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}"},
        stream=True,
        timeout=(5, 60),
    ) as resp:
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        for raw in resp.iter_lines():
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip().decode("utf-8")
            if data == "[DONE]":
                break
            event = json.loads(data)
            if "error" in event:
                return {"error": f"SSE: {event['error']}"}
            warnings += (event.get("gateway") or {}).get("warnings") or []
            usage = event.get("usage") or usage
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    content.append(piece)
                    if t_content is None:
                        t_content = time.monotonic() - t0
                finish = choice.get("finish_reason") or finish
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return {
        "content": "".join(content),
        "finish": finish,
        "t_first_content": t_content,
        "total": time.monotonic() - t0,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "cached_tokens": prompt_details.get("cached_tokens", 0),
        "reasoning_tokens": details.get("reasoning_tokens", 0),
        "cost": usage.get("cost", 0.0),
        "warnings": warnings,
    }


def load_turns(path: Path, limit: int) -> list[dict]:
    """Прочитать `synthetic_turns.jsonl`, по одному ходу на строку."""
    lines = path.read_text(encoding="utf-8").splitlines()
    turns = [json.loads(line) for line in lines if line.strip()]
    return turns[:limit] if limit else turns


def parse_call(text: str) -> tuple[str, dict] | None:
    """Разобрать `{"tool": ..., "args": {...}}`, снимая markdown-fence при необходимости."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("tool"), str):
        return None
    args = parsed.get("args")
    return parsed["tool"], args if isinstance(args, dict) else {}


def pct(values: list[float], q: float) -> float | None:
    """Перцентиль `q` (0..1) по `values`, `None` для пустого списка."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def fmt(value: float | None, scale: float = 1.0, digits: int = 2) -> str:
    """Отформатировать число с фиксированной точностью, `"-"` для `None`."""
    return "-" if value is None else f"{value * scale:.{digits}f}"


def main() -> int:  # noqa: C901, PLR0915 -- скрипт для ручного прогона, не библиотека
    """Прогнать все ходы `--turns` по всем `--variants` `--repeats` раз, напечатать сводку."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--turns", type=Path, default=Path(__file__).with_name("synthetic_turns.jsonl")
    )
    parser.add_argument("--base", default="https://api.selectel.ru/aig/v1")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    parser.add_argument("--variants", default="plain", help=f"через запятую из {list(VARIANTS)}")
    parser.add_argument("--limit", type=int, default=0, help="первые N ходов, 0 = все")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="с запасом, чтобы видеть реальную длину reasoning",
    )
    parser.add_argument(
        "--gate",
        default="json_object",
        choices=["json_object", "json_schema", "off"],
        help="фаза действия: свободный JSON | JSON со схемой (enum tools_allowed) | без формата",
    )
    args = parser.parse_args()

    key = os.environ.get("AIG_KEY", "")
    if not key:
        print("AIG_KEY не задан", file=sys.stderr)
        return 1
    variant_names = [name.strip() for name in args.variants.split(",") if name.strip()]
    unknown = [name for name in variant_names if name not in VARIANTS]
    if unknown:
        print(f"неизвестные варианты: {unknown}; есть: {list(VARIANTS)}", file=sys.stderr)
        return 1

    if not args.turns.is_file():
        print(
            f"нет файла {args.turns}: положи synthetic_turns.jsonl рядом со скриптом "
            "или укажи --turns",
            file=sys.stderr,
        )
        return 1
    turns = load_turns(args.turns, args.limit)
    print(f"ходов: {len(turns)}, вариантов: {len(variant_names)}, повторов: {args.repeats}\n")

    results: dict[tuple[str, str], list[dict]] = {}
    e2e: dict[str, list[dict]] = {}
    all_warnings: set[str] = set()

    def action_format(tools: list[str]) -> dict:
        if args.gate == "json_schema" and tools:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "tool_call",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "enum": tools},
                            "args": {"type": "object"},
                        },
                        "required": ["tool", "args"],
                        "additionalProperties": False,
                    },
                },
            }
        return {"type": "json_object"}

    def call(
        variant: str, phase: str, messages: list[dict], tools: list[str] | None = None
    ) -> dict:
        payload = {
            **VARIANTS[variant],
            "model": args.model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": args.max_tokens,
            "messages": messages,
        }
        if phase == "action":
            payload["temperature"] = 0.0
            if args.gate != "off":
                payload["response_format"] = action_format(tools or [])
        else:
            payload["temperature"] = 0.6
            payload["frequency_penalty"] = 0.4
        try:
            res = stream_once(args.base, key, payload)
        except requests.RequestException as error:
            res = {"error": str(error)[:200]}
        if "error" not in res:
            all_warnings.update(res["warnings"])
            res["ok"] = bool(res["content"].strip())
        results.setdefault((variant, phase), []).append(res)
        return res

    for turn in turns:
        mode = turn.get("mode", "baseline")
        for repeat in range(args.repeats):
            shift = repeat % len(variant_names)
            for variant in variant_names[shift:] + variant_names[:shift]:
                act = call(variant, "action", turn["action_req"], turn.get("tools_allowed"))
                if "error" in act:
                    print(f"{turn['id']:>11} {variant:12} action ERR {act['error']}")
                    e2e.setdefault(variant, []).append({"error": True})
                    continue
                parsed = parse_call(act["content"])
                act["ok"] = parsed is not None
                tool, call_args = parsed if parsed else (None, {})
                act["match"] = tool == turn.get("expected_tool")
                inline_text = str(call_args.get("text", "")).strip() if tool == "reply" else ""
                inline = mode == "inline" and bool(inline_text)
                print(
                    f"{turn['id']:>11} {variant:12} action ok={act['ok']!s:5} "
                    f"reason={act['reasoning_tokens']:4} "
                    f"first={fmt(act['t_first_content'])}s total={act['total']:.2f}s "
                    f"cached={act['cached_tokens']}/{act['prompt_tokens']} "
                    f"want={turn.get('expected_tool')} "
                    f"got={tool}"
                    + (f" | INLINE {len(inline_text)}ch: {inline_text[:90]!r}" if inline else "")
                    + (
                        ""
                        if parsed
                        else f" | RAW finish={act['finish']}: {act['content'][:300]!r}"
                    )
                )
                if inline:
                    total = act["total"]
                else:
                    # baseline-нода рвёт стрим на "tool": "reply" -> фаза действия ~ первый content
                    is_reply_baseline = mode == "baseline" and tool == "reply"
                    action_part = act["t_first_content"] if is_reply_baseline else act["total"]
                    reply_res = (
                        call(variant, "answer", turn["answer_req"])
                        if turn.get("answer_req")
                        else None
                    )
                    if reply_res is None or "error" in reply_res or action_part is None:
                        e2e.setdefault(variant, []).append({"error": True})
                        continue
                    total = action_part + reply_res["total"]
                    reply_first = fmt(reply_res["t_first_content"])
                    print(
                        f"{'':>11} {variant:12} answer reason={reply_res['reasoning_tokens']:4} "
                        f"first={reply_first}s total={reply_res['total']:.2f}s "
                        f"| {reply_res['content'][:90]!r}"
                    )
                e2e.setdefault(variant, []).append({"total": total, "inline": inline})

    print("\n=== итог ===")
    print(
        f"{'variant':12} {'phase':6} {'n':>3} {'err':>3} {'ok%':>4} {'tool%':>5}  "
        f"{'reason p50/p95/max':>18}  {'first p50/p95, s':>16}  {'total p50':>9}  "
        f"{'cached%':>7}  {'cost ₽':>7}"
    )
    for (variant, phase), items in results.items():
        good = [r for r in items if "error" not in r]
        reasons = [r["reasoning_tokens"] for r in good]
        firsts = [
            r["t_first_content"] if r["t_first_content"] is not None else float("inf")
            for r in good
        ]
        totals = [r["total"] for r in good]
        prompt_sum = sum(r["prompt_tokens"] for r in good)
        cached_share = (
            100 * sum(r["cached_tokens"] for r in good) / prompt_sum if prompt_sum else 0.0
        )
        ok_share = 100 * sum(r["ok"] for r in good) / max(len(good), 1)
        matched = sum(r.get("match", False) for r in good)
        tool_cell = f"{100 * matched / max(len(good), 1):.0f}" if phase == "action" else "-"
        reason_p50 = fmt(pct(reasons, 0.5), digits=0)
        reason_p95 = fmt(pct(reasons, 0.95), digits=0)
        reason_cell = f"{reason_p50}/{reason_p95}/{max(reasons, default=0)}"
        first_cell = f"{fmt(pct(firsts, 0.5))}/{fmt(pct(firsts, 0.95))}"
        print(
            f"{variant:12} {phase:6} {len(items):>3} {len(items) - len(good):>3} "
            f"{ok_share:>4.0f} {tool_cell:>5}  "
            f"{reason_cell:>18}  {first_cell:>16}  "
            f"{fmt(statistics.median(totals) if totals else None):>9}  "
            f"{cached_share:>7.0f}  {sum(r['cost'] for r in good):>7.3f}"
        )
    print("\nот конца реплики посетителя до готового текста для speak() (без TTS):")
    for variant, items in e2e.items():
        good = [i["total"] for i in items if "error" not in i]
        inline_share = 100 * sum(i.get("inline", False) for i in items) / max(len(items), 1)
        p50, p95 = fmt(pct(good, 0.5)), fmt(pct(good, 0.95))
        worst = fmt(max(good, default=None))
        print(
            f"  {variant:12} n={len(items)} err={len(items) - len(good)} "
            f"p50={p50}s p95={p95}s max={worst}s "
            f"inline={inline_share:.0f}%"
        )
    if all_warnings:
        print("\ngateway warnings:", *sorted(all_warnings), sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
