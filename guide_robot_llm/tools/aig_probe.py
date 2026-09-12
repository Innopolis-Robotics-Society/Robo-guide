#!/usr/bin/env python3
"""Проба AI Router: какой параметр реально выключает reasoning и сколько ждать первого content.

AIG_KEY=sk-sl-v1-... python3 aig_probe.py
AIG_KEY=... python3 aig_probe.py --extra '{"thinking":{"type":"disabled"}}' --max-tokens 160
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

PROMPT = 'Верни ТОЛЬКО JSON {"tool":"reply","args":{}}'

VARIANTS: list[tuple[str, dict, str]] = [
    ("baseline", {}, ""),
    ("effort=none", {"reasoning": {"effort": "none"}}, ""),
    ("effort=minimal", {"reasoning": {"effort": "minimal"}}, ""),
    ("reasoning.max_tokens=0", {"reasoning": {"max_tokens": 0}}, ""),
    ("thinking=disabled", {"thinking": {"type": "disabled"}}, ""),
    (
        "thinking=disabled+json",
        {"thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}},
        "",
    ),
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}, ""),
    ("/nothink", {}, " /nothink"),  # codespell:ignore nothink -- буквальный суффикс промпта модели
]


def probe(base: str, key: str, model: str, extra: dict, suffix: str, max_tokens: int) -> dict:
    """Отправить один запрос с `extra` поверх payload, вернуть тайминги/reasoning/usage."""
    payload = {
        **extra,
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": PROMPT + suffix}],
    }
    t0 = time.monotonic()
    t_reason = t_content = None
    content: list[str] = []
    reasoning_chars = 0
    finish = ""
    gateway: dict = {}
    usage: dict = {}
    with requests.post(
        f"{base.rstrip('/')}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}"},
        stream=True,
        timeout=(5, 60),
    ) as resp:
        if resp.status_code != 200:
            return {"http": resp.status_code, "body": resp.text[:300]}
        for raw in resp.iter_lines():
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip().decode("utf-8")
            if data == "[DONE]":
                break
            event = json.loads(data)
            if "error" in event:
                return {"sse_error": event["error"]}
            gateway = event.get("gateway") or gateway
            usage = event.get("usage") or usage
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if reasoning:
                    reasoning_chars += len(reasoning)
                    if t_reason is None:
                        t_reason = time.monotonic() - t0
                if delta.get("content"):
                    content.append(delta["content"])
                    if t_content is None:
                        t_content = time.monotonic() - t0
                finish = choice.get("finish_reason") or finish
    return {
        "finish": finish,
        "reasoning_chars": reasoning_chars,
        "t_first_reasoning": None if t_reason is None else round(t_reason, 2),
        "t_first_content": None if t_content is None else round(t_content, 2),
        "total": round(time.monotonic() - t0, 2),
        "content": "".join(content)[:120],
        "gateway": gateway,
        "usage": usage,
    }


def main() -> int:
    """Прогнать `VARIANTS` (или один `--extra`) по очереди, напечатать по строке на вариант."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://api.selectel.ru/aig/v1")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--extra", help="JSON: проверить одну комбинацию вместо списка")
    args = parser.parse_args()

    key = os.environ.get("AIG_KEY", "")
    if not key:
        print("AIG_KEY не задан", file=sys.stderr)
        return 1

    variants = [("custom", json.loads(args.extra), "")] if args.extra else VARIANTS
    for name, extra, suffix in variants:
        try:
            result = probe(args.base, key, args.model, extra, suffix, args.max_tokens)
        except requests.RequestException as error:
            result = {"exception": str(error)}
        print(f"{name:24} {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
