#!/usr/bin/env python3
"""Choose temporal confirmation length on training validation caches only.

Never reads a live test session. Creates a new model bundle without changing the
source weights/config. Both threshold and confirmation count are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_frames import supervised_indices
    from .train_wakeword_trial import DATA, choose_threshold, resolve_feature_cache
except ImportError:
    from train_wakeword_frames import supervised_indices
    from train_wakeword_trial import DATA, choose_threshold, resolve_feature_cache


def main():
    """Оценить подтверждение по соседним кадрам на сохранённых scores."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    import onnxruntime as ort

    source = json.loads((args.experiment / "results.json").read_text())
    cache = Path(source["training_cache"])
    real = resolve_feature_cache(cache)
    plan = json.loads((cache / "split_plan.json").read_text())
    model_path = args.experiment / "firaya_trial.onnx"
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])

    def scores(values):
        return np.concatenate(
            [
                session.run(None, {"audio_features": values[i : i + 256].astype(np.float32)})[
                    0
                ].reshape(-1)
                for i in range(0, len(values), 256)
            ]
        )

    positives = [
        scores(np.load(real / "features" / f"val_positive_{i:03d}.npy"))
        for i in range(len(plan["val"]["positive"]))
    ]
    negatives = [
        scores(np.load(real / "features" / f"val_{Path(p).stem}.npy"))
        for p in plan["val"]["negative_sources"]
    ]
    for row in json.loads((cache / "context_augmentation.json").read_text()):
        if row["split"] != "val":
            continue
        values = np.load(cache / "features" / f"context_{row['id']}.npy")
        if row["kind"] == "positive":
            positive, _ = supervised_indices(len(values), *row["target_interval"])
            positives.append(scores(values[positive]))
        else:
            negatives.append(scores(values))
    root = Path(json.loads((cache / "synthetic_plan.json").read_text())["source_root"])
    samples = {
        sample["file"]: sample
        for sample in json.loads((root / "plan.json").read_text())["samples"]
    }
    for row in json.loads((cache / "synthetic_augmentation.json").read_text()):
        if row["split"] != "val":
            continue
        relative, variant = row["source"].removeprefix("silero/").split("#")
        identity = hashlib.sha256(f"1337:{samples[relative]['id']}:{variant}".encode()).hexdigest()
        negatives.append(scores(np.load(cache / "features" / f"synthetic_{identity}.npy")))
    for natural in sorted(args.experiment.glob("natural*")):
        if not natural.is_dir():
            continue
        root = Path(json.loads((natural / "natural_plan.json").read_text())["source_root"])
        samples = {
            sample["file"]: sample
            for sample in json.loads((root / "plan.json").read_text())["samples"]
        }
        timings = json.loads((natural / "timings.json").read_text())
        for row in json.loads((natural / "synthetic_augmentation.json").read_text()):
            if row["split"] != "val":
                continue
            relative, variant = row["source"].removeprefix("silero/").split("#")
            sample = samples[relative]
            identity = hashlib.sha256(f"1337:{sample['id']}:{variant}".encode()).hexdigest()
            values = np.load(natural / "features" / f"synthetic_{identity}.npy")
            if sample["kind"] == "negative":
                negatives.append(scores(values))
            else:
                candidates = timings[relative]["candidates"]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    pos, _ = supervised_indices(len(values), candidate["start"], candidate["end"])
                    if len(pos):
                        positives.append(scores(values[pos]))
    trials = []
    for count in range(2, 7):
        metric = choose_threshold(positives, negatives, 0.00001, count)
        metric["consecutive_frames"] = count
        trials.append(metric)
        print(metric, flush=True)
    best = min(
        trials, key=lambda row: (row["false_events"], -row["hits"], row["consecutive_frames"])
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name in (
        "firaya_trial.onnx",
        "firaya_trial.pt",
        "split_plan.json",
        "feature_provenance.json",
    ):
        (args.output / name).write_bytes((args.experiment / name).read_bytes())
    config = json.loads((args.experiment / "inference_config.json").read_text())
    config.update(threshold=best["threshold"], consecutive_frames=best["consecutive_frames"])
    (args.output / "inference_config.json").write_text(json.dumps(config, indent=2))
    result = {
        "source_experiment": str(args.experiment),
        "source_model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "selection": best,
        "trials": trials,
        "selection_data": "train-time validation only",
        "evaluation_sessions_used_in_training": [],
        "ready_for_production": False,
    }
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
