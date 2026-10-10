#!/usr/bin/env python3
"""Time-localized frame training using existing train/val context caches only.

No live session is accepted by this CLI. Exact timing of synthetic concatenations
is known, quiet real crop timing remains approximate. Weak crowded positives and
multiword TTS positives are excluded from frame supervision, not mislabeled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import (
        DATA,
        FRAME,
        RATE,
        build_model,
        choose_threshold,
        evaluate,
        export_onnx,
        predict_features,
        prepare_features,
        read_wav,
        resolve_feature_cache,
    )
except ImportError:
    from train_wakeword_trial import (
        DATA,
        FRAME,
        RATE,
        build_model,
        choose_threshold,
        evaluate,
        export_onnx,
        predict_features,
        prepare_features,
        read_wav,
        resolve_feature_cache,
    )


def supervised_indices(count, start, end):
    """Выбрать положительные и отрицательные кадры по границам имени."""
    times = (np.arange(count) + 1) * FRAME / RATE
    positive = np.flatnonzero((times >= end + 0.4) & (times <= end + 1.0))
    negative = np.flatnonzero((times < start) | (times > end + 2.0))
    return positive, negative


def main():
    """Обучить кадровую голову и сохранить оценку по выборкам."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--width", type=int, default=64)
    parser.add_argument("--architecture", choices=("dnn", "tcn"), default="dnn")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--learning-rate", type=float, default=0.0003)
    parser.add_argument(
        "--score-margin",
        type=float,
        default=0.00001,
        help="validation-only threshold margin, larger than checked ONNX error",
    )
    parser.add_argument("--continuous", type=Path, action="append")
    parser.add_argument("--reuse-continuous-features", type=Path, action="append")
    args = parser.parse_args()
    if (
        args.epochs < 1
        or args.width < 1
        or not np.isfinite(args.learning_rate)
        or args.learning_rate <= 0
        or not np.isfinite(args.score_margin)
        or args.score_margin < 1e-6
    ):
        parser.error("epochs/width/lr must be positive; finite score margin >= 1e-6")
    if args.architecture == "tcn" and args.width % 8:
        parser.error("TCN width must be divisible by 8")
    if args.reuse_continuous_features and not args.continuous:
        parser.error("--reuse-continuous-features requires --continuous")
    if len(args.reuse_continuous_features or []) > len(args.continuous or []):
        parser.error("too many reuse caches for continuous datasets")
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    import torch

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(1337)
    rng = np.random.default_rng(1337)
    plan = json.loads((args.cache / "split_plan.json").read_text())
    backbone = DATA / "experiments/_backbone"
    raw = DATA / "raw"
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "split_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    provenance = json.loads((args.cache / "feature_provenance.json").read_text())
    if provenance["backbone"] != json.loads((backbone / "provenance.json").read_text()):
        raise ValueError("backbone changed")
    before = {
        row["source"]: hashlib.sha256((raw / row["source"]).read_bytes()).hexdigest()
        for split in plan.values()
        for row in split["positive"]
    }
    if any(
        before[row["source"]] != row["source_sha256"]
        for split in plan.values()
        for row in split["positive"]
    ):
        raise ValueError("real source changed")
    features = prepare_features(
        plan, raw, args.output, backbone, 1337, resolve_feature_cache(args.cache)
    )
    positive_frames, negative_sources = [], []
    for record in features["train"]["positive"]:
        row = record["row"]
        if "quiet_" not in row["source"]:
            continue
        start, end = row["crop_start_seconds"] + 0.35, row["crop_end_seconds"] - 0.35
        pos, neg = supervised_indices(len(record["features"]), start, end)
        if len(pos):
            positive_frames.append(record["features"][pos])
        if len(neg):
            negative_sources.append(record["features"][neg])
    negative_sources.extend(record["features"] for record in features["train"]["negative"])
    contextual = {split: {"positive": [], "negative": []} for split in ("train", "val")}
    context_rows = json.loads((args.cache / "context_augmentation.json").read_text())
    for row in context_rows:
        values = np.load(args.cache / "features" / f"context_{row['id']}.npy", allow_pickle=False)
        if row["kind"] == "positive":
            pos, neg = supervised_indices(len(values), *row["target_interval"])
            if row["split"] == "train":
                if len(pos):
                    positive_frames.append(values[pos])
                if len(neg):
                    negative_sources.append(values[neg])
            # Only name-local scores may satisfy validation recall.
            contextual[row["split"]]["positive"].append({"features": values[pos], "row": row})
        else:
            contextual[row["split"]]["negative"].append({"features": values})
            if row["split"] == "train":
                negative_sources.append(values)
    synthetic_root = Path(
        json.loads((args.cache / "synthetic_plan.json").read_text())["source_root"]
    )
    samples_by_file = {
        sample["file"]: sample
        for sample in json.loads((synthetic_root / "plan.json").read_text())["samples"]
    }
    synthetic_rows = json.loads((args.cache / "synthetic_augmentation.json").read_text())
    validation_negative = []
    for row in synthetic_rows:
        source, variant = row["source"].split("#")
        relative = source.removeprefix("silero/")
        # Find cache identity in the existing manifest rather than a new random id.
        sample = samples_by_file.get(relative)
        if sample is None:
            raise ValueError("unknown synthetic source")
        identity = hashlib.sha256(f"1337:{sample['id']}:{variant}".encode()).hexdigest()
        values = np.load(args.cache / "features" / f"synthetic_{identity}.npy", allow_pickle=False)
        if sample["kind"] == "negative":
            if row["split"] == "train":
                negative_sources.append(values)
            else:
                validation_negative.append(values)
        elif "a_positive_01_" in relative or "a_positive_02_" in relative:
            audio = read_wav(synthetic_root / relative)
            active = np.flatnonzero(
                np.abs(audio.astype(float)) > max(100, np.abs(audio.astype(float)).max() * 0.02)
            )
            pos, neg = supervised_indices(len(values), active[0] / RATE, active[-1] / RATE)
            if len(pos):
                positive_frames.append(values[pos])
            if len(neg):
                negative_sources.append(values[neg])
    natural_val_positive, natural_val_negative = [], []
    if args.continuous:
        try:
            from .wakeword_natural_training import load_natural
        except ImportError:
            from wakeword_natural_training import load_natural
        for number, source_root in enumerate(args.continuous):
            caches = args.reuse_continuous_features or []
            reuse = caches[number] if number < len(caches) else None
            destination = args.output / ("natural" if number == 0 else f"natural_{number:02d}")
            natural, timings = load_natural(source_root, destination, raw, plan, backbone, reuse)
            for split, groups in natural.items():
                for record in groups["negative"]:
                    (negative_sources if split == "train" else natural_val_negative).append(
                        record["features"]
                    )
                for record in groups["positive"]:
                    source = record["row"]["source"].split("#")[0].removeprefix("silero/")
                    candidates = timings[source]["candidates"]
                    if len(candidates) != 1:
                        continue
                    candidate = candidates[0]
                    pos, neg = supervised_indices(
                        len(record["features"]), candidate["start"], candidate["end"]
                    )
                    if split == "train":
                        if len(pos):
                            positive_frames.append(record["features"][pos])
                        if len(neg):
                            negative_sources.append(record["features"][neg])
                    elif len(pos):
                        natural_val_positive.append(record["features"][pos])
    positives = np.concatenate(positive_frames).astype(np.float32)
    mean = positives[:, -1, :].mean(0).astype(np.float32)
    std = np.maximum(positives[:, -1, :].std(0), 0.1).astype(np.float32)
    model = build_model(mean, std, args.width, True, args.architecture)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=0.01)
    print(
        f"Frame supervision: {len(positives)} name windows, {len(negative_sources)} negative "
        f"sources",
        flush=True,
    )
    history, best, best_state = [], None, None
    for epoch in range(1, args.epochs + 1):
        model.train()
        loss_sum = 0
        for _step in range(100):
            positive = positives[rng.integers(0, len(positives), 64)]
            negative = []
            for number in rng.integers(0, len(negative_sources), 192):
                value = negative_sources[number]
                negative.append(value[int(rng.integers(0, len(value)))])
            batch = torch.from_numpy(
                np.concatenate([positive, np.asarray(negative)]).astype(np.float32)
            )
            labels = torch.cat([torch.full((64,), 0.98), torch.full((192,), 0.02)])
            logits = model(batch).reshape(-1)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, labels)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            loss_sum += float(loss.detach())
        pos_scores = [
            predict_features(model, record["features"]) for record in features["val"]["positive"]
        ]
        pos_scores += [
            predict_features(model, record["features"]) for record in contextual["val"]["positive"]
        ]
        pos_scores += [predict_features(model, values) for values in natural_val_positive]
        neg_scores = [
            predict_features(model, record["features"]) for record in features["val"]["negative"]
        ]
        neg_scores += [predict_features(model, values) for values in validation_negative]
        neg_scores += [
            predict_features(model, record["features"]) for record in contextual["val"]["negative"]
        ]
        neg_scores += [predict_features(model, values) for values in natural_val_negative]
        metric = choose_threshold(pos_scores, neg_scores, args.score_margin)
        metric.update(epoch=epoch, loss=loss_sum)
        key = (metric["false_events"], -metric["hits"])
        if best is None or key < best["key"]:
            best = {"key": key, **metric}
            best_state = {
                name: value.detach().clone() for name, value in model.state_dict().items()
            }
        history.append(metric)
        print(
            f"epoch {epoch}: val {metric['hits']}/{metric['total']}, false "
            f"{metric['false_events']}, threshold {metric['threshold']:.4f}",
            flush=True,
        )
    model.load_state_dict(best_state)
    torch.save(model.state_dict(), args.output / "firaya_trial.pt")
    error = export_onnx(model, args.output)
    metrics = {split: evaluate(model, features[split], best["threshold"])[0] for split in features}
    result = {
        "method": "name-local frame supervision; context-spliced human names; label smoothing",
        "head_width": args.width,
        "layer_norm": True,
        "architecture": args.architecture,
        "score_margin": args.score_margin,
        "selection": {k: v for k, v in best.items() if k != "key"},
        "metrics": metrics,
        "training_cache": str(args.cache),
        "evaluation_sessions_used_in_training": [],
        "positive_frame_count": len(positives),
        "negative_sources": len(negative_sources),
        "natural_validation_positive": len(natural_val_positive),
        "natural_validation_negative": len(natural_val_negative),
        "onnx_max_absolute_error": error,
        "ready_for_production": False,
    }
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (args.output / "training_history.json").write_text(json.dumps(history, indent=2))
    (args.output / "inference_config.json").write_text(
        json.dumps(
            {
                "model": "firaya_trial.onnx",
                "threshold": best["threshold"],
                "consecutive_frames": 2,
                "cooldown_seconds": 2,
                "feature_frames": 16,
                "feature_dim": 96,
                "production": False,
            },
            indent=2,
        )
    )
    (args.output / "feature_provenance.json").write_text(
        json.dumps({**provenance, "reused_from": str(resolve_feature_cache(args.cache))}, indent=2)
    )
    if any(
        hashlib.sha256((raw / source).read_bytes()).hexdigest() != digest
        for source, digest in before.items()
    ):
        raise ValueError("real source changed during training")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
