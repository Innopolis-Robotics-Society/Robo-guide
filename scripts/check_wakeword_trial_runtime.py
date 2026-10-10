#!/usr/bin/env python3
"""Проверить зафиксированный ONNX-прототип через реальный streaming API openWakeWord.

Не обучает модель и не меняет порог по test. Сравнивает результаты с исходным
PyTorch-тестом. Без ROS, записи с микрофона и изменения production-конфигурации.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import DATA, FRAME, PREFIX, RATE, TAIL, event_indices, read_wav
except ImportError:
    from train_wakeword_trial import DATA, FRAME, PREFIX, RATE, TAIL, event_indices, read_wav


def main() -> int:
    """Оценить неизменную модель на отложенных positive/negative в потоковом режиме."""
    from openwakeword.model import Model

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--raw-root", type=Path, default=DATA / "raw")
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    args = parser.parse_args()
    experiment = args.experiment
    model_path = experiment / "firaya_trial.onnx"
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    result = json.loads((experiment / "results.json").read_text())
    split = json.loads((experiment / "split_plan.json").read_text())["test"]
    threshold = result["selection"]["threshold"]
    model = Model(
        wakeword_models=[str(model_path)],
        inference_framework="onnx",
        melspec_model_path=str(args.backbone / "melspectrogram.onnx"),
        embedding_model_path=str(args.backbone / "embedding_model.onnx"),
        ncpu=1,
    )
    name = model_path.stem
    samples_processed = 0

    def scores(audio):
        nonlocal samples_processed
        model.reset()
        values = []
        for first in range(0, len(audio), FRAME):
            chunk = audio[first : first + FRAME]
            if len(chunk) < FRAME:
                chunk = np.pad(chunk, (0, FRAME - len(chunk)))
            score = float(model.predict(chunk)[name])
            samples_processed += FRAME
            if (first + FRAME) / RATE > PREFIX:
                values.append(score)
        return np.asarray(values)

    started = time.monotonic()
    negative = []
    backgrounds = []
    for source in split["negative_sources"]:
        audio = read_wav(args.raw_root / source)
        backgrounds.append(audio)
        values = scores(audio)
        negative.append(
            {
                "source": source,
                "false_events": len(event_indices(values, threshold)),
                "event_seconds": [
                    PREFIX + (index + 1) * FRAME / RATE
                    for index in event_indices(values, threshold)
                ],
            }
        )
    context = np.concatenate(backgrounds)
    with (experiment / "positive_predictions.csv").open(encoding="utf-8") as stream:
        expected = {
            row["source"]: row["detected"] == "True"
            for row in csv.DictReader(stream)
            if row["split"] == "test"
        }
    positive = []
    for index, row in enumerate(split["positive"]):
        audio = read_wav(args.raw_root / row["source"])
        offset = (index * 11713) % (len(context) - int((PREFIX + TAIL) * RATE))
        stream = np.concatenate(
            [
                context[offset : offset + int(PREFIX * RATE)],
                audio,
                context[offset + int(PREFIX * RATE) : offset + int((PREFIX + TAIL) * RATE)],
            ]
        )
        detected = bool(event_indices(scores(stream), threshold))
        if detected != expected[row["source"]]:
            raise RuntimeError(f"streaming/PyTorch mismatch: {row['source']}")
        positive.append({"source": row["source"], "detected": detected})
    false_events = sum(row["false_events"] for row in negative)
    if false_events != result["metrics"]["test"]["false_events"]:
        raise RuntimeError("streaming/PyTorch mismatch on negative")
    elapsed = time.monotonic() - started
    validation = {
        "api": "openwakeword.Model.predict, ONNX, 1280-sample streaming frames",
        "threshold_unchanged": threshold,
        "model_sha256": model_hash,
        "test_positive_detected": sum(row["detected"] for row in positive),
        "test_positive_total": len(positive),
        "test_negative_false_events": false_events,
        "matches_original_evaluation": True,
        "wall_seconds": elapsed,
        "audio_seconds_processed": samples_processed / RATE,
        "processing_real_time_factor": elapsed / (samples_processed / RATE),
        "negative": negative,
        "positive": positive,
    }
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != model_hash:
        raise RuntimeError("модель изменилась во время проверки")
    target = experiment / "runtime_validation.json"
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(validation, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                key: value
                for key, value in validation.items()
                if key not in ("positive", "negative")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
