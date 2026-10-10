#!/usr/bin/env python3
"""Проверить deploy runtime на frozen live-сессии; никакого обучения или ROS."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guide_robot_voice"))
from guide_robot_voice.lib.wakeword_runtime import (  # noqa: E402 -- source checkout import
    FrozenWakewordModel,
    WakewordStream,
)


def check(bundle: Path, reference: Path) -> dict:
    """16 ms chunks должны дать те же scores/срабатывания, что старые 80 ms."""
    model = FrozenWakewordModel(bundle)
    session = json.loads((reference / "session.json").read_text())
    if session["model_sha256"] != model.manifest["sha256"]["firaya_trial.onnx"]:
        raise ValueError("Модель не совпадает с reference-session")
    if session["inference_config"] != model.manifest["inference_config"]:
        raise ValueError("Параметры не совпадают с reference-session")
    sources = json.loads((reference / "results.json").read_text())["takes"]
    rows, timings = [], []
    audio_seconds = 0.0
    started = time.perf_counter()
    for take in sources:
        path = reference / take["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != take["wav_sha256"]:
            raise ValueError(f"WAV изменён: {path}")
        with wave.open(str(path)) as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError(f"Нужен mono PCM16 @ 16kHz: {path}")
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()
        stream = WakewordStream(model, session["inference_config"])
        events, scores = [], []
        for first in range(0, len(audio), 256):
            block_started = time.perf_counter()
            events.extend(
                stream.push("offline-test", first, first / 16000, audio[first : first + 256])
            )
            elapsed = time.perf_counter() - block_started
            if (first + 256) % 1280 == 0:
                timings.append(elapsed)
                scores.append(stream.last_score)
        expected_scores = json.loads(
            (reference / "takes" / f'{take["key"]}_scores.json').read_text()
        )
        expected = expected_scores["scores"][: len(scores)]
        difference = float(np.max(np.abs(np.asarray(scores) - expected)))
        times = [event.end_sample / 16000 for event in events]
        if difference > 1e-5 or not np.allclose(times, take["event_seconds"], atol=1e-7):
            raise ValueError(f'Deploy/reference mismatch: {take["key"]}, score diff={difference}')
        audio_seconds += len(audio) / 16000
        rows.append({"key": take["key"], "events": times, "max_score_difference": difference})
    wall = time.perf_counter() - started
    return {
        "model_sha256": session["model_sha256"],
        "reference_session": reference.name,
        "all_events_and_scores_match": True,
        "takes": rows,
        "audio_seconds": audio_seconds,
        "wall_seconds": wall,
        "real_time_factor": wall / audio_seconds,
        "inference_chunk_ms_p95": float(np.quantile(timings, 0.95) * 1000),
        "inference_chunk_ms_max": max(timings) * 1000,
        "note": "Offline runtime parity; not a hardware/AEC acceptance test",
    }


def main(argv=None):
    """Сохранить независимый runtime parity report, не трогая live-сессию."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "guide_robot_voice/models/firaya")
    parser.add_argument("--reference-session", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = check(args.bundle, args.reference_session)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "takes"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
