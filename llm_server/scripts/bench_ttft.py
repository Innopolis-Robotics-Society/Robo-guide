#!/usr/bin/env python3
"""Меряет TTFT и decode tok/s против OpenAI-совместимого /v1/chat/completions.

Только stdlib -- скрипт должен запускаться и на голом хосте без venv (в т.ч.
с Jetson по Wi-Fi для второго обязательного прогона, см. SPEC §3/§8). SSE
разбирается вручную: llama.cpp стримит `data: {...}\\n\\n`, `data: [DONE]` в
конце -- никакой библиотеки для этого не требуется.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions",
                    help="адрес /v1/chat/completions (default: %(default)s)")
    p.add_argument("--n", type=int, default=11,
                    help="число итераций, первая отбрасывается как холодный кэш (default: %(default)s)")
    p.add_argument("--prompt-file", default=None,
                    help="файл с промптом (default: встроенная короткая фраза)")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--api-key", default="")
    return p.parse_args()


def load_prompt(path: str | None) -> str:
    if path is None:
        return "Расскажи коротко о себе."
    with open(path, encoding="utf-8") as f:
        return f.read()


def run_once(url: str, prompt: str, max_tokens: int, api_key: str) -> dict:
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    t_start = time.monotonic()
    ttft = None
    t_first = None
    n_tokens = 0

    with urllib.request.urlopen(req) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            content = (choices[0].get("delta") or {}).get("content")
            if content:
                now = time.monotonic()
                if ttft is None:
                    ttft = now - t_start
                    t_first = now
                # Каждый непустой content-delta -- один шаг декодирования в
                # streaming-режиме llama.cpp; для tok/s этого достаточно.
                n_tokens += 1

    t_end = time.monotonic()
    decode_time = (t_end - t_first) if t_first is not None else 0.0
    tok_s = (n_tokens / decode_time) if decode_time > 0 else 0.0
    return {
        "ttft": ttft,
        "total": t_end - t_start,
        "tokens": n_tokens,
        "tok_s": tok_s,
    }


def pctl(data: list[float], p: int) -> float:
    if not data:
        return float("nan")
    if len(data) < 2:
        return data[0]
    return statistics.quantiles(data, n=100)[p - 1]


def main() -> None:
    args = parse_args()
    if args.n < 2:
        print("error: --n должно быть >= 2 (первая итерация отбрасывается как холодный кэш)",
              file=sys.stderr)
        sys.exit(1)

    prompt = load_prompt(args.prompt_file)
    measured = []

    for i in range(args.n):
        try:
            r = run_once(args.url, prompt, args.max_tokens, args.api_key)
        except urllib.error.URLError as e:
            print(f"[{i + 1}/{args.n}] запрос не удался: {e}", file=sys.stderr)
            sys.exit(1)

        tag = "прогрев (отброшен)" if i == 0 else "измерение"
        ttft_ms = r["ttft"] * 1000 if r["ttft"] is not None else float("nan")
        print(f"[{i + 1}/{args.n}] {tag}: TTFT={ttft_ms:.1f}ms "
              f"total={r['total'] * 1000:.1f}ms tokens={r['tokens']} decode={r['tok_s']:.1f}tok/s")

        if i > 0:
            measured.append(r)

    ttfts = [r["ttft"] * 1000 for r in measured if r["ttft"] is not None]
    tok_ss = [r["tok_s"] for r in measured]
    totals = [r["total"] * 1000 for r in measured]

    print()
    print(f"N измерений (без прогрева): {len(measured)}")
    print(f"TTFT   p50={pctl(ttfts, 50):.1f}ms   p95={pctl(ttfts, 95):.1f}ms")
    print(f"decode p50={pctl(tok_ss, 50):.1f}tok/s   p95={pctl(tok_ss, 95):.1f}tok/s")
    print(f"total  p50={pctl(totals, 50):.1f}ms   p95={pctl(totals, 95):.1f}ms")


if __name__ == "__main__":
    main()
