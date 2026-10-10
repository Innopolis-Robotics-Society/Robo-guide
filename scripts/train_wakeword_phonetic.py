#!/usr/bin/env python3
"""Train spectral GRU KWS with auxiliary Russian character CTC on existing caches.

No evaluation session path is accepted. All gradients use original train speaker,
split-local synthetic texts/backgrounds only. Live tests remain read-only checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import DATA, choose_threshold
    from .wakeword_mel import BANDS, CONTEXT, FRONTEND_VERSION, HOP, RATE, feature_window
    from .wakeword_phonetic import (
        ALPHABET,
        build_model,
        consecutive_groups,
        encode_text,
        reviewed_interval,
        word_positions,
    )
    from .wakeword_synthetic_training import load_synthetic
except ImportError:
    from train_wakeword_trial import DATA, choose_threshold
    from wakeword_mel import BANDS, CONTEXT, FRONTEND_VERSION, HOP, RATE, feature_window
    from wakeword_phonetic import (
        ALPHABET,
        build_model,
        consecutive_groups,
        encode_text,
        reviewed_interval,
        word_positions,
    )
    from wakeword_synthetic_training import load_synthetic


def prepare(cache, output, complete_word=False):
    """Подготовить признаки и разметку из сохранённого плана выборок."""
    result = {
        split: {kind: [] for kind in ("positive", "negative", "ctc")} for split in ("train", "val")
    }
    plan = json.loads((cache / "split_plan.json").read_text())
    original = {row["source"]: row for split in plan.values() for row in split["positive"]}
    context_cache = Path(json.loads((cache / "results.json").read_text())["cache"])
    synthetic_root = Path(
        json.loads((context_cache / "synthetic_plan.json").read_text())["source_root"]
    )
    _, synthetic = load_synthetic(synthetic_root)
    samples = {row["sample"]["file"]: row["sample"] for row in synthetic}
    contexts = {
        "context/" + row["id"]: row
        for row in json.loads((context_cache / "context_augmentation.json").read_text())
    }
    natural_cache = Path(json.loads((cache / "results.json").read_text())["natural_cache"])
    natural = {}
    for directory in natural_cache.glob("natural*"):
        if directory.is_dir():
            root = Path(json.loads((directory / "natural_plan.json").read_text())["source_root"])
            _, rows = load_synthetic(root)
            natural.update({str(root / row["sample"]["file"]): row["sample"] for row in rows})
    rows = json.loads((cache / "feature_manifest.json").read_text())
    sources = {
        **{source: row["source_sha256"] for source, row in original.items()},
        **{row["source"]: row["sha256"] for row in rows if row["source"].startswith("negative/")},
    }
    for source, digest in sources.items():
        if hashlib.sha256((DATA / "raw" / source).read_bytes()).hexdigest() != digest:
            raise ValueError("raw source changed")
    used = []
    for row in rows:
        split, source, interval = row["split"], row["source"], row["interval"]
        text, first, last = None, 200, None
        if source in original:
            interval = reviewed_interval(original[source])
            if interval is None:
                continue
            first = 200 + round(original[source]["crop_start_seconds"] * RATE / HOP)
            last = 200 + round(original[source]["crop_end_seconds"] * RATE / HOP)
            text = "фирая"
        elif source.startswith("silero/"):
            sample = samples[source.removeprefix("silero/").split("#")[0]]
            if sample.get("training_split", "train") != split:
                raise ValueError("synthetic split changed")
            text = sample["text"]
            last = -128
        elif source in natural:
            sample = natural[source]
            if sample["training_split"] != split:
                raise ValueError("natural split changed")
            text = sample["text"]
            last = -128
        elif source in contexts:
            context = contexts[source]
            if original[context["source"]]["method"] == "full_recording":
                continue
            left, right, substitute = (
                samples[part["file"]]["text"] for part in context["context_sources"]
            )
            center = "фирая" if context["kind"] == "positive" else substitute
            text = " ".join(
                [
                    left if context["position"] != "begin" else "",
                    center,
                    right if context["position"] != "end" else "",
                ]
            )
            last = -128
        elif not source.startswith("negative/"):
            raise ValueError(f"unknown feature source: {source}")
        mel = np.load(cache / "features" / row["file"], allow_pickle=False)
        if mel.ndim != 2 or mel.shape[1] != BANDS or not np.isfinite(mel).all():
            raise ValueError("bad cached mel features")
        pos, neg = word_positions(len(mel), interval, complete_word)
        if len(pos):
            result[split]["positive"].append({"mel": mel, "positions": pos, "source": source})
        for group in consecutive_groups(neg):
            result[split]["negative"].append({"mel": mel, "positions": group, "source": source})
        if text:
            labels = encode_text(text)
            body = mel[first:last]
            required = len(labels) + int(np.sum(labels[1:] == labels[:-1]))
            if len(body) >= 20 and (len(body) + 3) // 4 >= required:
                result[split]["ctc"].append({"mel": body, "labels": labels, "source": source})
        used.append(
            {
                **row,
                "corrected_interval": interval,
                "feature_sha256": hashlib.sha256(
                    (cache / "features" / row["file"]).read_bytes()
                ).hexdigest(),
            }
        )
    (output / "feature_manifest.json").write_text(json.dumps(used, ensure_ascii=False, indent=2))
    (output / "split_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return result


def main():
    """Обучить фонетическую голову с фиксированным планом выборок."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--warmup-epochs", type=int, default=6)
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--ctc-weight", type=float, default=0.2)
    parser.add_argument("--time-stride", type=int, choices=(2, 4), default=4)
    parser.add_argument("--stretch-context", type=Path)
    parser.add_argument(
        "--negative-candidates",
        type=int,
        default=1,
        help="mine the strongest among this many positions per sampled negative source",
    )
    parser.add_argument("--complete-word-supervision", action="store_true")
    parser.add_argument("--learning-rate", type=float, default=0.0005)
    args = parser.parse_args()
    if (
        args.epochs < 1
        or args.steps < 1
        or args.warmup_epochs < 0
        or not 0 <= args.ctc_weight <= 10
        or not 1 <= args.negative_candidates <= 16
        or not np.isfinite(args.learning_rate)
        or args.learning_rate <= 0
    ):
        parser.error("invalid training parameters")
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    import onnxruntime as ort
    import torch
    from threadpoolctl import threadpool_limits
    from torch import nn

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(1337)
    _limiter = threadpool_limits(limits=1, user_api="blas")
    rng = np.random.default_rng(1337)
    args.output.mkdir(parents=True, exist_ok=False)
    records = prepare(args.cache, args.output, args.complete_word_supervision)
    if args.stretch_context:
        extra_plan = json.loads((args.stretch_context / "plan.json").read_text())
        if (
            extra_plan["split_plan_sha256"]
            != hashlib.sha256((args.cache / "split_plan.json").read_bytes()).hexdigest()
        ):
            raise ValueError("stretch augmentation belongs to another split plan")
        for row in json.loads((args.stretch_context / "manifest.json").read_text()):
            path = args.stretch_context / row["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("stretch feature changed")
            mel = np.load(path, allow_pickle=False)
            pos, neg = word_positions(
                len(mel),
                row["target_interval"] if row["kind"] == "positive" else None,
                args.complete_word_supervision,
            )
            if row["kind"] == "positive":
                if len(pos):
                    records[row["split"]]["positive"].append(
                        {"mel": mel, "positions": pos, "source": row["file"]}
                    )
            for group in consecutive_groups(neg):
                records[row["split"]]["negative"].append(
                    {"mel": mel, "positions": group, "source": row["file"]}
                )
    arrays = [record["mel"] for record in records["train"]["ctc"]]
    count = sum(len(value) for value in arrays)
    mean = sum(value.sum(0, dtype=np.float64) for value in arrays) / count
    var = sum((value.astype(np.float64) ** 2).sum(0) for value in arrays) / count - mean**2
    std = np.maximum(np.sqrt(np.maximum(var, 0)), 1)
    model = build_model(mean, std, args.time_stride)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=0.01)
    ctc_loss = nn.CTCLoss(blank=0, zero_infinity=False)
    print(
        "Records",
        {
            split: {kind: len(group) for kind, group in data.items()}
            for split, data in records.items()
        },
        flush=True,
    )

    def sample(kind, count):
        group = records["train"][kind]
        return np.stack(
            [
                feature_window(record["mel"], int(rng.choice(record["positions"])))
                for record in [group[index] for index in rng.integers(0, len(group), count)]
            ]
        )

    def negative_batch(count):
        if args.negative_candidates == 1:
            return sample("negative", count)
        group = records["train"]["negative"]
        chosen = [group[index] for index in rng.integers(0, len(group), count)]
        candidates = np.stack(
            [
                feature_window(record["mel"], int(rng.choice(record["positions"])))
                for record in chosen
                for _ in range(args.negative_candidates)
            ]
        )
        training = model.training
        model.eval()
        with torch.no_grad():
            logits = (
                model(torch.from_numpy(candidates))
                .flatten()
                .numpy()
                .reshape(count, args.negative_candidates)
            )
        model.train(training)
        strongest = logits.argmax(1)
        # Keep some random negatives, so mining cannot forget ordinary background.
        strongest[: count // 4] = 0
        candidates = candidates.reshape(count, args.negative_candidates, CONTEXT, BANDS)
        return candidates[np.arange(count), strongest]

    def character_loss(count=8):
        group = records["train"]["ctc"]
        selected = [group[index] for index in rng.integers(0, len(group), count)]
        # Force real quiet names into every batch without ever sampling val/test speakers.
        quiet = [record for record in group if record["source"].startswith("positive/")]
        selected[:2] = [quiet[int(rng.integers(len(quiet)))] for _ in range(2)]
        length = max(len(record["mel"]) for record in selected)
        batch = np.broadcast_to(mean.astype(np.float32), (count, length, BANDS)).copy()
        for index, record in enumerate(selected):
            batch[index, : len(record["mel"])] = record["mel"]
        logprob = model.ctc(torch.from_numpy(batch)).transpose(0, 1)
        targets = torch.from_numpy(np.concatenate([record["labels"] for record in selected]))
        input_lengths = torch.tensor(
            [
                (len(record["mel"]) + args.time_stride - 1) // args.time_stride
                for record in selected
            ]
        )
        target_lengths = torch.tensor([len(record["labels"]) for record in selected])
        return ctc_loss(logprob, targets, input_lengths, target_lengths)

    def update(loss):
        if not torch.isfinite(loss):
            raise ValueError("nonfinite phonetic training loss")
        optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 5)
        optimizer.step()
        return float(loss.detach())

    for epoch in range(1, args.warmup_epochs + 1):
        model.train()
        loss = sum(update(character_loss()) for _ in range(args.steps))
        print(f"CTC warmup {epoch}: {loss / args.steps:.4f}", flush=True)

    def predict(record):
        scores = []
        with torch.no_grad():
            for first in range(0, len(record["positions"]), 96):
                batch = np.stack(
                    [
                        feature_window(record["mel"], int(end))
                        for end in record["positions"][first : first + 96]
                    ]
                )
                scores.extend(
                    torch.sigmoid(model(torch.from_numpy(batch))).numpy().reshape(-1).tolist()
                )
        return np.asarray(scores)

    best, state, history = None, None, []
    for epoch in range(1, args.epochs + 1):
        model.train()
        loss_sum = 0
        for _ in range(args.steps):
            batch = torch.from_numpy(np.concatenate([sample("positive", 24), negative_batch(48)]))
            labels = torch.cat([torch.full((24,), 0.98), torch.full((48,), 0.02)])
            loss = nn.functional.binary_cross_entropy_with_logits(model(batch).flatten(), labels)
            if args.ctc_weight:
                loss = loss + args.ctc_weight * character_loss()
            loss_sum += update(loss)
        if epoch == 1 or epoch % 5 == 0 or epoch == args.epochs:
            model.eval()
            metric = choose_threshold(
                [predict(record) for record in records["val"]["positive"]],
                [predict(record) for record in records["val"]["negative"]],
                margin=0.00001,
            )
            metric.update(epoch=epoch, loss=loss_sum / args.steps)
            history.append(metric)
            key = (metric["false_events"], -metric["hits"])
            if best is None or key < best["key"]:
                best = {"key": key, **metric}
                state = {
                    name: value.detach().clone() for name, value in model.state_dict().items()
                }
            print(
                f"epoch {epoch}: phonetic val {metric['hits']}/{metric['total']}, false "
                f"{metric['false_events']}, threshold {metric['threshold']:.4f}",
                flush=True,
            )
    model.load_state_dict(state)
    model.eval()
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
    runtime = ort.InferenceSession(str(args.output / "firaya_trial.onnx"), options)
    batch = sample("positive", 8)
    with torch.no_grad():
        expected = probability(torch.from_numpy(batch)).numpy()
    error = float(np.abs(expected - runtime.run(None, {"audio_features": batch})[0]).max())
    if error > 1e-5:
        raise ValueError("ONNX numerical mismatch")
    selection = {key: value for key, value in best.items() if key != "key"}
    (args.output / "results.json").write_text(
        json.dumps(
            {
                "method": "frequency-preserving CNN GRU with auxiliary Russian character CTC",
                "selection": selection,
                "onnx_max_absolute_error": error,
                "cache": str(args.cache),
                "parameters": vars(args)
                | {
                    "cache": str(args.cache),
                    "output": str(args.output),
                    "stretch_context": str(args.stretch_context) if args.stretch_context else None,
                },
                "model_parameters": sum(p.numel() for p in model.parameters()),
                "ctc_alphabet": ALPHABET,
                "evaluation_sessions_used_in_training": [],
                "ready_for_production": False,
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
