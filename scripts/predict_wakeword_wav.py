#!/usr/bin/env python3
"""Проверить KWS на новом WAV без переобучения и искусственного хвоста тишины."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import DATA, FRAME, PREFIX, RATE, event_indices, read_wav
except ImportError:
    from train_wakeword_trial import DATA, FRAME, PREFIX, RATE, event_indices, read_wav


def streaming_scores(model, name: str, audio: np.ndarray) -> np.ndarray:
    """Warm up with 2s silence before the file, never add silence after it.

    Only the final incomplete 80ms frame is zero-padded as required by the API.
    """
    model.reset()
    for _ in range(round(PREFIX * RATE / FRAME)):
        model.predict(np.zeros(FRAME, dtype=np.int16))
    scores = []
    for first in range(0, len(audio), FRAME):
        chunk = audio[first : first + FRAME]
        if len(chunk) < FRAME:
            chunk = np.pad(chunk, (0, FRAME - len(chunk)))
        scores.append(float(model.predict(chunk)[name]))
    return np.asarray(scores)


def main(argv=None):
    """Проверить неизменную модель на выбранном WAV-файле."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    args = parser.parse_args(argv)
    dependencies = DATA / "experiments/_python_deps"
    if dependencies.is_dir():
        sys.path.insert(0, str(dependencies))
    try:
        from .test_wakeword_interactive import load_runtime
    except ImportError:
        from test_wakeword_interactive import load_runtime

    config = json.loads((args.experiment / "inference_config.json").read_text())
    path = args.experiment / config["model"]
    model = load_runtime(path, config, args.backbone)
    audio = read_wav(args.audio)
    scores = streaming_scores(model, path.stem, audio)
    events = event_indices(
        scores,
        config["threshold"],
        config["cooldown_seconds"],
        config.get("consecutive_frames", 2),
    )
    print(f"Модель: {path}")
    print(f"Запись: {len(audio) / RATE:.2f} с; порог: {config['threshold']:.3f}")
    print(f"Максимальный score: {max(scores, default=0):.3f}")
    if not events:
        print("Имя не обнаружено.")
    else:
        print("Срабатывания — секунды от начала записи:")
        for index in events:
            print(f"  ФИРАЯ: {min((index + 1) * FRAME / RATE, len(audio) / RATE):.2f} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
