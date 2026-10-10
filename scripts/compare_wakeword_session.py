#!/usr/bin/env python3
"""Compare frozen ONNX models on saved live WAVs without training or overwriting.

Uses the official streaming API, saves all scores and hashes. Optional ASR-token
diagnostics localize the expected name; these are not human-verified labels.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

try:
    from .test_wakeword_interactive import encode, infer_record, load_runtime, write_atomic
    from .train_wakeword_trial import DATA
except ImportError:
    from test_wakeword_interactive import encode, infer_record, load_runtime, write_atomic
    from train_wakeword_trial import DATA


def words_with_times(asr):
    """Восстановить слова и их временные границы из CTC-подсказок."""
    text, times = "", []
    for token, time in zip(asr.get("tokens", []), asr.get("timestamps", []), strict=False):
        token = token.replace("▁", " ")
        text += token
        times.extend([time] * len(token))
    return [
        {
            "word": match.group(),
            "start": times[match.start()],
            "end": times[match.end() - 1] + 0.08,
        }
        for match in re.finditer(r"[а-яё]+", text.lower())
    ]


NAME_ALIASES = {
    "фирая",
    "фирайя",
    "феррая",
    "ферая",
    "ферайя",
    "феррайя",
    "ферраи",
    "феррайи",
    "фрая",
    "вчерая",
    "сирая",
    "черая",
    "серая",
    "сырая",
    "феррари",
}


def align_script_targets(text, words):
    """Align the whole reading script, so genuine Ferrari distractors stay negative.

    Alignment is only an ASR hint, never human-verified truth. Missing or differently
    decoded names stay ambiguous. Exact surrounding words anchor each occurrence.
    """
    expected = re.findall(r"[а-яё]+", text.lower())
    observed = [word["word"] for word in words]
    rows, columns = len(expected) + 1, len(observed) + 1
    costs = [[float(i + j) for j in range(columns)] for i in range(rows)]
    moves = [[None] * columns for _ in range(rows)]
    for i in range(1, rows):
        for j in range(1, columns):
            name_match = expected[i - 1] == "фирая" and observed[j - 1] in NAME_ALIASES
            substitution = 0 if expected[i - 1] == observed[j - 1] else 0.2 if name_match else 1
            options = (
                (costs[i - 1][j - 1] + substitution, "match"),
                (costs[i - 1][j] + 1, "delete"),
                (costs[i][j - 1] + 1, "insert"),
            )
            costs[i][j], moves[i][j] = min(options, key=lambda value: value[0])
    aligned = {}
    i, j = rows - 1, columns - 1
    while i and j:
        move = moves[i][j]
        if move == "match":
            aligned[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move == "delete":
            i -= 1
        else:
            j -= 1
    result = []
    for index, word in enumerate(expected):
        if word != "фирая":
            continue
        observed_index = aligned.get(index)
        candidates = (
            [words[observed_index]]
            if observed_index is not None and observed[observed_index] in NAME_ALIASES
            else []
        )
        result.append(
            {
                "script_word_index": index,
                "candidates": candidates,
                "ambiguous": len(candidates) != 1,
                "method": "script_sequence_alignment",
            }
        )
    return result


def timing_hints(record, asr):
    """Map script-positive slots to phonetic ASR hints, never to KWS predictions."""
    words = words_with_times(asr)
    result = []
    cues = record["capture"]["cues"]
    for index, cue in enumerate(cues):
        if not cue["contains_wakeword"]:
            continue
        start = cue["actual_seconds"]
        end = (
            cues[index + 1]["actual_seconds"]
            if index + 1 < len(cues)
            else record["requested_seconds"]
        )
        if cue.get("expected_events", 1) > 1:
            slot_words = [word for word in words if start <= word["start"] < end]
            result.extend(
                {"cue_index": index, "cue_text": cue["text"], **target}
                for target in align_script_targets(cue["text"], slot_words)
            )
            continue
        candidates = [
            word for word in words if start <= word["start"] < end and word["word"] in NAME_ALIASES
        ]
        result.append(
            {
                "cue_index": index,
                "cue_text": cue["text"],
                "candidates": candidates,
                "ambiguous": len(candidates) != 1,
            }
        )
    return result


def assess_timing(events, hints):
    """Сопоставить события с временными подсказками без повторного учёта."""
    used, hits = set(), 0
    details = []
    for hint in hints:
        candidates = hint["candidates"]
        matches = []
        if len(candidates) == 1:
            # Fixed allowance for embedding + confirmation latency, same for
            # every model. Do not stretch these windows to make a model pass.
            word = candidates[0]
            matches = [
                i
                for i, event in enumerate(events)
                if i not in used and word["start"] <= event <= word["end"] + 1.28
            ]
        if matches:
            hits += 1
            used.add(matches[0])
        details.append({**hint, "matched_event_seconds": events[matches[0]] if matches else None})
    return {
        "hits": hits,
        "total": len(hints),
        "unmatched_events": [event for i, event in enumerate(events) if i not in used],
        "ambiguous_targets": sum(hint["ambiguous"] for hint in hints),
        "label_verified": False,
        "targets": details,
    }


def main(argv=None):
    """Сравнить неизменные модели на одной сохранённой сессии."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--experiment", required=True, type=Path, action="append")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    args = parser.parse_args(argv)
    session = json.loads((args.session / "session.json").read_text())
    records = [
        json.loads(path.read_text())
        for path in sorted((args.session / "takes").glob("*.json"))
        if not path.stem.endswith("_scores")
    ]
    records = [record for record in records if record["status"] == "complete"]
    analysis = {}
    if args.analysis:
        data = json.loads((args.analysis / "analysis.json").read_text())
        if Path(data["session"]).resolve() != args.session.resolve():
            raise ValueError("ASR analysis belongs to another session")
        analysis = {row["key"]: row for row in data["records"]}
    for record in records:
        digest = hashlib.sha256((args.session / record["file"]).read_bytes()).hexdigest()
        if digest != record["wav_sha256"] or (
            record["key"] in analysis and analysis[record["key"]]["wav_sha256"] != digest
        ):
            raise ValueError("recording hash mismatch")
    for name, digest in session["backbone_sha256"].items():
        if hashlib.sha256((args.backbone / name).read_bytes()).hexdigest() != digest:
            raise ValueError("backbone hash mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    page = [
        "<!doctype html><meta charset='utf-8'><title>Сравнение KWS</title>",
        "<style>body{font:17px sans-serif;margin:30px}"
        "td,th{border:1px solid #aaa;padding:8px}table{border-collapse:collapse}</style>",
        "<h1>Сравнение моделей на сохранённых тестах</h1>",
        "<p>Регрессионная проверка, не независимый финальный тест. WAV не использованы в "
        "обучении. "
        "Метки по сценарию и подсказкам ASR, не проверены человеком. Совпадение количества не "
        "доказывает точность.</p>",
    ]
    import os

    for index, experiment in enumerate(args.experiment):
        config = json.loads((experiment / "inference_config.json").read_text())
        model_path = experiment / config["model"]
        model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        runtime = load_runtime(model_path, config, args.backbone)
        target = args.output / f"model_{index:02d}"
        target.mkdir()
        rows = []
        for record in records:
            result, curve = infer_record(runtime, config, args.session / record["file"], record)
            if record["key"] in analysis:
                result["timing_assessment"] = assess_timing(
                    result["event_seconds"], timing_hints(record, analysis[record["key"]]["asr"])
                )
            write_atomic(target / f"{record['key']}_scores.json", encode(curve))
            rows.append(result)
        metric = {
            "experiment": str(experiment),
            "model_sha256": model_hash,
            "config": config,
            "takes": rows,
            "negative_test_events": sum(
                row["observed_events"] for row in rows if not row["expected_events"]
            ),
            "cue_hits": sum(row["assessment"]["estimated_hits"] for row in rows),
            "cue_misses": sum(row["assessment"]["estimated_misses"] for row in rows),
            "cue_extra_events": sum(row["assessment"]["estimated_extra_events"] for row in rows),
            "timing_hits": sum(row.get("timing_assessment", {}).get("hits", 0) for row in rows),
            "timing_ambiguous": sum(
                row.get("timing_assessment", {}).get("ambiguous_targets", 0) for row in rows
            ),
        }
        write_atomic(target / "results.json", encode(metric))
        results.append(metric)
        print(
            experiment.name,
            {
                key: metric[key]
                for key in (
                    "negative_test_events",
                    "cue_hits",
                    "cue_misses",
                    "cue_extra_events",
                    "timing_hits",
                    "timing_ambiguous",
                )
            },
            flush=True,
        )
        page.append(
            f"<h2>{html.escape(experiment.name)}</h2><p>Порог {config['threshold']:.4f}; "
            f"активаций на отрицательных тестах: {metric['negative_test_events']}.</p>"
            "<table><tr><th>Дубль</th><th>Запись</th><th>Ожидалось</th><th>Активации, "
            "с</th><th>Совпадения по ASR-времени</th></tr>"
        )
        for row in rows:
            audio = html.escape(
                os.path.relpath(args.session / row["file"], args.output), quote=True
            )
            timed = row.get("timing_assessment", {})
            page.append(
                f"<tr><td>{row['key']}</td><td><audio controls preload='none' "
                f"src='{audio}'></audio></td>"
                f"<td>{row['expected_events']}</td><td>{row['event_seconds']}</td>"
                f"<td>{timed.get('hits', '?')}/{timed.get('total', '?')}</td></tr>"
            )
        page.append(
            f"</table><p><a href='model_{index:02d}/results.json'>Полные результаты, метки и хэш "
            f"модели</a></p>"
        )
    write_atomic(
        args.output / "results.json", encode({"session": str(args.session), "results": results})
    )
    write_atomic(args.output / "review.html", "\n".join(page).encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
