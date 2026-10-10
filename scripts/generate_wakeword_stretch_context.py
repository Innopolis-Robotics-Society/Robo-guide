#!/usr/bin/env python3
"""Generate split-local fast HUMAN-name context features, with paired negatives.

No live evaluation recordings are accepted or read. Existing reviewed quiet raw
names are shortened without intentional pitch shift. Negative substitute words
receive the same operation and are selected with similar original duration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import DATA, read_wav
    from .wakeword_context_training import assemble, trim_silence
    from .wakeword_mel import FRONTEND_VERSION, spectrogram
    from .wakeword_stretch import time_stretch
    from .wakeword_synthetic_training import augment_stream, load_synthetic, split_for_sample
except ImportError:
    from train_wakeword_trial import DATA, read_wav
    from wakeword_context_training import assemble, trim_silence
    from wakeword_mel import FRONTEND_VERSION, spectrogram
    from wakeword_stretch import time_stretch
    from wakeword_synthetic_training import augment_stream, load_synthetic, split_for_sample


def main():
    """Подготовить варианты темпа и контекста из существующего кэша."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    from threadpoolctl import threadpool_limits

    _limiter = threadpool_limits(limits=1, user_api="blas")
    plan = json.loads((args.cache / "split_plan.json").read_text())
    original = {row["source"]: row for data in plan.values() for row in data["positive"]}
    root = Path(json.loads((args.cache / "results.json").read_text())["cache"])
    synthetic_root = Path(json.loads((root / "synthetic_plan.json").read_text())["source_root"])
    _, synthetic = load_synthetic(synthetic_root)
    pool = {split: [] for split in ("train", "val")}
    for row in synthetic:
        if row["sample"]["kind"] == "negative" and "_word_" in row["sample"]["file"]:
            word = trim_silence(read_wav(synthetic_root / row["sample"]["file"]))
            pool[split_for_sample(row["sample"])].append((row, word))
    background = {
        split: np.concatenate(
            [read_wav(DATA / "raw" / path) for path in plan[split]["negative_sources"]]
        )
        for split in pool
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "features").mkdir()
    settings = {
        "split_plan_sha256": hashlib.sha256(
            (args.cache / "split_plan.json").read_bytes()
        ).hexdigest(),
        "rates": [1.3, 1.6, 1.9],
        "frontend_version": FRONTEND_VERSION,
        "method": "phase vocoder; label-symmetric transform, duration-matched substitutes",
        "source_cache": str(args.cache),
        "evaluation_sources": [],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "stretch_script_sha256": hashlib.sha256(
            Path(time_stretch.__code__.co_filename).read_bytes()
        ).hexdigest(),
    }
    (args.output / "plan.json").write_text(json.dumps(settings, indent=2))
    manifest = []
    for context in json.loads((root / "context_augmentation.json").read_text()):
        if context["kind"] != "positive":
            continue
        original_row = original[context["source"]]
        if original_row["method"] == "full_recording":
            continue
        source = DATA / "raw" / context["source"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != original_row["source_sha256"]:
            raise ValueError("original human name changed")
        name = trim_silence(
            read_wav(source)[original_row["crop_start_sample"] : original_row["crop_end_sample"]]
        )
        left, right = (
            trim_silence(read_wav(synthetic_root / part["file"]))
            for part in context["context_sources"][:2]
        )
        split = context["split"]
        nearest = sorted(pool[split], key=lambda pair: abs(len(pair[1]) - len(name)))[:20]
        for rate in settings["rates"]:
            seed = int(
                hashlib.sha256(f"stretch-v1:{context['id']}:{rate}".encode()).hexdigest()[:8], 16
            )
            rng = np.random.default_rng(seed)
            substitute_row, substitute = nearest[int(rng.integers(len(nearest)))]
            for kind, center in (("positive", name), ("negative", substitute)):
                body, interval = assemble(
                    time_stretch(center, rate), left, right, context["position"]
                )
                stream, details = augment_stream(body, background[split], "crowd_12db", seed)
                value = spectrogram(stream)
                filename = f"features/stretch_{len(manifest):05d}.npy"
                np.save(args.output / filename, value)
                manifest.append(
                    {
                        "file": filename,
                        "sha256": hashlib.sha256(
                            (args.output / filename).read_bytes()
                        ).hexdigest(),
                        "split": split,
                        "kind": kind,
                        "target_interval": list(interval),
                        "rate": rate,
                        "position": context["position"],
                        "human_source": context["source"],
                        "human_sha256": original_row["source_sha256"],
                        "context_sources": context["context_sources"][:2],
                        "substitute_source": substitute_row["sample"]["file"],
                        "substitute_sha256": substitute_row["sha256"],
                        "background_sources": plan[split]["negative_sources"],
                        **details,
                    }
                )
        if len(manifest) % 120 == 0:
            print("Stretch context features", len(manifest), flush=True)
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("Completed", args.output, len(manifest), flush=True)


if __name__ == "__main__":
    main()
