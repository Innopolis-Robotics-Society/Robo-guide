#!/usr/bin/env python3
"""Train a direct log-mel KWS head, preserving train/val and live-test separation.

Reconstruct the recorded augmentation manifests rather than use live test audio.
All feature caches are new, raw WAVs and previous models are read-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from train_wakeword_trial import DATA, choose_threshold, read_wav
from wakeword_context_training import assemble, trim_silence
from wakeword_mel import (
    BANDS,
    CONTEXT,
    FRONTEND_VERSION,
    HOP,
    RATE,
    build_model,
    feature_window,
    spectrogram,
)
from wakeword_synthetic_training import augment_stream, load_synthetic


def frame_positions(mel, interval=None):
    """Выбрать концы mel-окон по интервалу произнесённого имени."""
    ends = np.arange(8, len(mel) + 1, 8)
    times = ends * HOP / RATE - 2
    if interval is None:
        return np.zeros(0, int), ends[times > 0]
    start, end = interval
    positive = ends[(times >= end + 0.08) & (times <= end + 0.4)]
    negative = ends[(times > 0) & ((times < start) | (times > end + CONTEXT * HOP / RATE))]
    return positive, negative


def prepare(args, output):
    """Подготовить признаки и разметку из сохранённого плана выборок."""
    from scipy.signal import resample_poly

    cache = args.cache
    plan = json.loads((cache / "split_plan.json").read_text())
    raw = DATA / "raw"
    records = {
        split: {kind: [] for kind in ("positive", "negative")} for split in ("train", "val")
    }
    background = {
        split: np.concatenate([read_wav(raw / path) for path in plan[split]["negative_sources"]])
        for split in records
    }
    directory = output / "features"
    directory.mkdir()
    provenance = []
    counter = 0

    def add(split, audio, interval, metadata):
        nonlocal counter
        values = spectrogram(audio)
        name = f"mel_{counter:05d}.npy"
        np.save(directory / name, values)
        pos, neg = frame_positions(values, interval)
        if len(pos):
            records[split]["positive"].append({"mel": values, "positions": pos})
        if len(neg):
            records[split]["negative"].append({"mel": values, "positions": neg})
        provenance.append({"file": name, "split": split, "interval": interval, **metadata})
        counter += 1
        if counter % 200 == 0:
            print(f"Mel features: {counter}", flush=True)

    for split in records:
        for source in plan[split]["negative_sources"]:
            audio = read_wav(raw / source)
            add(
                split,
                audio,
                None,
                {
                    "source": source,
                    "sha256": hashlib.sha256((raw / source).read_bytes()).hexdigest(),
                },
            )
        for index, row in enumerate(plan[split]["positive"]):
            if "quiet_" not in row["source"]:
                continue
            audio = read_wav(raw / row["source"])
            if (
                hashlib.sha256((raw / row["source"]).read_bytes()).hexdigest()
                != row["source_sha256"]
            ):
                raise ValueError("real source changed")
            offset = (index * 11713) % (len(background[split]) - round(3.28 * RATE))
            stream = np.concatenate(
                [
                    background[split][offset : offset + 32000],
                    audio,
                    background[split][offset + 32000 : offset + round(3.28 * RATE)],
                ]
            )
            add(
                split,
                stream,
                (row["crop_start_seconds"] + 0.35, row["crop_end_seconds"] - 0.35),
                {
                    "source": row["source"],
                    "sha256": row["source_sha256"],
                    "timing": "reviewed crop minus padding, approximate",
                },
            )
    root = Path(json.loads((cache / "synthetic_plan.json").read_text())["source_root"])
    _, synth_rows = load_synthetic(root)
    samples = {row["sample"]["file"]: row for row in synth_rows}
    for row in json.loads((cache / "synthetic_augmentation.json").read_text()):
        relative, variant = row["source"].removeprefix("silero/").split("#")
        sample = samples[relative]["sample"]
        if sample["kind"] == "positive" and not any(
            token in relative for token in ("a_positive_01_", "a_positive_02_")
        ):
            continue
        audio = read_wav(root / relative)
        interval = None
        if sample["kind"] == "positive":
            active = np.flatnonzero(
                np.abs(audio.astype(float)) > max(100, np.abs(audio.astype(float)).max() * 0.02)
            )
            interval = (active[0] / RATE, active[-1] / RATE)
        stream, _ = augment_stream(audio, background[row["split"]], variant, row["seed"])
        add(
            row["split"],
            stream,
            interval,
            {"source": row["source"], "sha256": row["source_sha256"], "seed": row["seed"]},
        )
    real_rows = {row["source"]: row for split in plan.values() for row in split["positive"]}
    for row in json.loads((cache / "context_augmentation.json").read_text()):
        original = real_rows[row["source"]]
        audio = read_wav(raw / row["source"])[
            original["crop_start_sample"] : original["crop_end_sample"]
        ]
        target = trim_silence(audio)
        target = np.clip(
            np.rint(resample_poly(target, 100, row["name_speed_percent"])), -32768, 32767
        ).astype(np.int16)
        parts = [trim_silence(read_wav(root / value["file"])) for value in row["context_sources"]]
        body, interval = assemble(
            target if row["kind"] == "positive" else parts[2], parts[0], parts[1], row["position"]
        )
        variant = "clean" if row["position"] == "begin" else "crowd_12db"
        stream, _ = augment_stream(body, background[row["split"]], variant, row["seed"])
        add(
            row["split"],
            stream,
            interval if row["kind"] == "positive" else None,
            {
                "source": "context/" + row["id"],
                "sha256": row["source_sha256"],
                "seed": row["seed"],
            },
        )
    for natural in sorted(args.natural_cache.glob("natural*")):
        if not natural.is_dir():
            continue
        root = Path(json.loads((natural / "natural_plan.json").read_text())["source_root"])
        _, samples_list = load_synthetic(root)
        samples = {row["sample"]["file"]: row for row in samples_list}
        timings = json.loads((natural / "timings.json").read_text())
        for row in json.loads((natural / "synthetic_augmentation.json").read_text()):
            relative, variant = row["source"].removeprefix("silero/").split("#")
            sample = samples[relative]["sample"]
            interval = None
            if sample["kind"] == "positive":
                candidates = timings[relative]["candidates"]
                if len(candidates) != 1:
                    continue
                interval = (candidates[0]["start"], candidates[0]["end"])
            stream, _ = augment_stream(
                read_wav(root / relative), background[row["split"]], variant, row["seed"]
            )
            add(
                row["split"],
                stream,
                interval,
                {
                    "source": str(root / relative),
                    "sha256": row["source_sha256"],
                    "seed": row["seed"],
                },
            )
    (output / "feature_manifest.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2)
    )
    (output / "split_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return records


def main():
    """Обучить log-mel голову на подготовленных реальных и TTS-примерах."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--natural-cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--epochs", type=int, default=35)
    args = parser.parse_args()
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    import onnxruntime as ort
    import torch
    from threadpoolctl import threadpool_limits
    from torch import nn

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(1337)
    _limiter = threadpool_limits(limits=1, user_api="blas")
    args.output.mkdir(parents=True, exist_ok=False)
    records = prepare(args, args.output)
    all_train = [record["mel"] for group in records["train"].values() for record in group]
    count = sum(len(value) for value in all_train)
    mean = sum(value.sum(0, dtype=np.float64) for value in all_train) / count
    variance = sum((value.astype(np.float64) ** 2).sum(0) for value in all_train) / count - mean**2
    std = np.maximum(np.sqrt(np.maximum(variance, 0)), 1)
    model = build_model(mean, std)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0003, weight_decay=0.01)
    rng = np.random.default_rng(1337)

    def sample(kind, count):
        group = records["train"][kind]
        selected = rng.integers(0, len(group), count)
        return np.stack(
            [
                feature_window(group[index]["mel"], int(rng.choice(group[index]["positions"])))
                for index in selected
            ]
        )

    def predict(record):
        values = []
        model.eval()
        with torch.no_grad():
            for first in range(0, len(record["positions"]), 128):
                batch = np.stack(
                    [
                        feature_window(record["mel"], int(end))
                        for end in record["positions"][first : first + 128]
                    ]
                )
                values.extend(
                    torch.sigmoid(model(torch.from_numpy(batch))).numpy().reshape(-1).tolist()
                )
        return np.asarray(values)

    best, state, history = None, None, []
    for epoch in range(1, args.epochs + 1):
        model.train()
        loss_sum = 0
        for _step in range(100):
            batch = torch.from_numpy(
                np.concatenate([sample("positive", 32), sample("negative", 64)])
            )
            labels = torch.cat([torch.full((32,), 0.98), torch.full((64,), 0.02)])
            logits = model(batch).reshape(-1)
            loss = nn.functional.binary_cross_entropy_with_logits(logits, labels)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            loss_sum += float(loss.detach())
        if epoch == 1 or epoch % 5 == 0 or epoch == args.epochs:
            positives = [predict(record) for record in records["val"]["positive"]]
            negatives = [predict(record) for record in records["val"]["negative"]]
            metric = choose_threshold(positives, negatives, margin=0.00001)
            metric.update(epoch=epoch, loss=loss_sum)
            history.append(metric)
            key = (metric["false_events"], -metric["hits"])
            if best is None or key < best["key"]:
                best = {"key": key, **metric}
                state = {
                    name: value.detach().clone() for name, value in model.state_dict().items()
                }
            print(
                f"epoch {epoch}: mel val {metric['hits']}/{metric['total']}, false "
                f"{metric['false_events']}, threshold {metric['threshold']:.4f}",
                flush=True,
            )
    model.load_state_dict(state)
    torch.save(model.state_dict(), args.output / "firaya_trial.pt")
    probability = nn.Sequential(model, nn.Sigmoid()).eval()
    torch.onnx.export(
        probability,
        torch.zeros(1, CONTEXT, BANDS),
        str(args.output / "firaya_trial.onnx"),
        input_names=["audio_features"],
        output_names=["firaya"],
        opset_version=17,
        dynamo=False,
        dynamic_axes={"audio_features": {0: "batch"}, "firaya": {0: "batch"}},
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    onnx = ort.InferenceSession(str(args.output / "firaya_trial.onnx"), options)
    batch = sample("positive", 8)
    with torch.no_grad():
        expected = probability(torch.from_numpy(batch)).numpy()
    error = float(np.abs(expected - onnx.run(None, {"audio_features": batch})[0]).max())
    if error > 1e-5:
        raise ValueError("ONNX numerical mismatch")
    selection = {key: value for key, value in best.items() if key != "key"}
    (args.output / "results.json").write_text(
        json.dumps(
            {
                "method": "direct log-mel CNN, no English embedding model",
                "selection": selection,
                "onnx_max_absolute_error": error,
                "evaluation_sessions_used_in_training": [],
                "ready_for_production": False,
                "cache": str(args.cache),
                "natural_cache": str(args.natural_cache),
                "training_records": {kind: len(group) for kind, group in records["train"].items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (args.output / "training_history.json").write_text(json.dumps(history, indent=2))
    (args.output / "inference_config.json").write_text(
        json.dumps(
            {
                "model": "firaya_trial.onnx",
                "feature_kind": "logmel80",
                "frontend_version": FRONTEND_VERSION,
                "feature_frames": CONTEXT,
                "feature_dim": BANDS,
                "threshold": selection["threshold"],
                "consecutive_frames": 2,
                "cooldown_seconds": 2,
                "production": False,
            },
            indent=2,
        )
    )
    print("Completed", args.output, selection, flush=True)


if __name__ == "__main__":
    main()
