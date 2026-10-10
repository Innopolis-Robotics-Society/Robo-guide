"""Split-local speech-context augmentation; live evaluation files are never used.

Real, reviewed quiet-name crops are placed at the beginning/middle/end of speech.
Paired negatives have identical contexts but a non-target word in the middle.
Concatenation is a training approximation, not natural coarticulation. Validation
uses only its own human speaker, negative text families and background source.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

try:
    from .train_wakeword_trial import FRAME, RATE, read_wav, stream_features
    from .wakeword_synthetic_training import augment_stream, split_for_sample
except ImportError:
    from train_wakeword_trial import FRAME, RATE, read_wav, stream_features
    from wakeword_synthetic_training import augment_stream, split_for_sample


def trim_silence(audio, padding=0.04):
    """Trim low-energy edges of an already reviewed crop, not speech in a crowd."""
    values = audio.astype(np.float64)
    active = np.flatnonzero(np.abs(values) >= max(100, np.abs(values).max() * 0.02))
    if not len(active):
        raise ValueError("empty speech crop")
    margin = round(padding * RATE)
    return audio[max(0, active[0] - margin) : min(len(audio), active[-1] + margin + 1)].copy()


def equalize(audio, rms=3500):
    """Масштабировать контекст по RMS активных сэмплов."""
    values = audio.astype(np.float64)
    active = values[np.abs(values) > max(100, np.abs(values).max() * 0.08)]
    if not len(active):
        raise ValueError("silent context")
    values *= rms / np.sqrt(np.mean(active**2))
    return values


def assemble(name, left, right, position):
    """Return PCM and approximate name interval; transformations apply to both labels."""
    if position not in ("begin", "middle", "end"):
        raise ValueError("unknown placement")
    left = equalize(left) if position != "begin" else np.zeros(0)
    right = equalize(right) if position != "end" else np.zeros(0)
    name = equalize(name)
    stream = np.concatenate([left, name, right])
    scale = min(1, 30000 / max(1, np.abs(stream).max()))
    interval = (len(left) / RATE, (len(left) + len(name)) / RATE)
    return np.rint(stream * scale).astype("<i2"), interval


def frame_mask(count, interval):
    """Allow a pair near the name and normal embedding latency, not arbitrary speech."""
    times = (np.arange(count) + 1) * FRAME / RATE
    return (times >= interval[0]) & (times <= interval[1] + 1.2)


def add_context(
    features, plan, raw, synthetic_root, synthetic_rows, output, backbone, seed=1337, reuse=None
):
    """Добавить акустический контекст к синтетическим обучающим примерам."""
    from openwakeword.utils import AudioFeatures
    from scipy.signal import resample_poly

    extractor = AudioFeatures(
        melspec_model_path=str(backbone / "melspectrogram.onnx"),
        embedding_model_path=str(backbone / "embedding_model.onnx"),
        inference_framework="onnx",
        ncpu=1,
    )
    previous = {}
    if reuse:
        previous = {
            item["id"]: item
            for item in json.loads((reuse / "context_augmentation.json").read_text())
        }
    provenance = []
    added = {split: {"positive": [], "negative": []} for split in ("train", "val")}
    pools = {
        split: [
            row
            for row in synthetic_rows
            if row["sample"]["kind"] == "negative" and split_for_sample(row["sample"]) == split
        ]
        for split in ("train", "val")
    }
    for split in ("train", "val"):
        background = np.concatenate(
            [read_wav(raw / path) for path in plan[split]["negative_sources"]]
        )
        rows = [row for row in plan[split]["positive"] if "quiet_" in row["source"]]
        for index, row in enumerate(rows):
            source = raw / row["source"]
            audio = read_wav(source)
            if hashlib.sha256(source.read_bytes()).hexdigest() != row["source_sha256"]:
                raise ValueError("real source changed")
            name = trim_silence(audio[row["crop_start_sample"] : row["crop_end_sample"]])
            for variant, position in enumerate(("begin", "middle", "end")):
                identity = hashlib.sha256(
                    f"context-v1:{seed}:{row['source']}:{position}".encode()
                ).hexdigest()
                local_seed = int(identity[:8], 16)
                rng = np.random.default_rng(local_seed)
                chosen = [pools[split][i] for i in rng.integers(0, len(pools[split]), size=3)]
                chunks = []
                for chosen_row in chosen:
                    path = synthetic_root / chosen_row["sample"]["file"]
                    if hashlib.sha256(path.read_bytes()).hexdigest() != chosen_row["sha256"]:
                        raise ValueError("synthetic context source changed")
                    chunks.append(trim_silence(read_wav(path)))
                # All origins/rates remain split-local. The name is human, surrounding
                # speech is TTS; matched negatives use exactly the same splice structure.
                speed = (90, 100, 110)[variant]
                target = np.clip(np.rint(resample_poly(name, 100, speed)), -32768, 32767).astype(
                    "<i2"
                )
                left, right, substitute = chunks
                for kind, word in (("positive", target), ("negative", substitute)):
                    stream, interval = assemble(word, left, right, position)
                    noisy, details = augment_stream(
                        stream, background, "clean" if variant == 0 else "crowd_12db", local_seed
                    )
                    item_id = identity + "_" + kind
                    meta = {
                        "id": item_id,
                        "split": split,
                        "kind": kind,
                        "source": row["source"],
                        "source_sha256": row["source_sha256"],
                        "context_sources": [
                            {"file": x["sample"]["file"], "sha256": x["sha256"]} for x in chosen
                        ],
                        "position": position,
                        "name_speed_percent": speed,
                        "target_interval": list(interval),
                        "seed": local_seed,
                        "background_sources": plan[split]["negative_sources"],
                        "backbone": json.loads((backbone / "provenance.json").read_text()),
                        **details,
                    }
                    filename = f"context_{item_id}.npy"
                    cached = reuse / "features" / filename if reuse else None
                    reused = cached and cached.is_file() and previous.get(item_id) == meta
                    if reused:
                        values = np.load(cached, allow_pickle=False)
                    else:
                        values = stream_features(extractor, noisy, 2)
                    if (
                        values.ndim != 3
                        or values.shape[1:] != (16, 96)
                        or not np.isfinite(values).all()
                    ):
                        raise ValueError("invalid context feature cache")
                    if reused:
                        (output / "features" / filename).hardlink_to(cached)
                    else:
                        np.save(output / "features" / filename, values)
                    record = {
                        "features": values,
                        "source": "context/" + item_id,
                        "whole_bag": True,
                        "evaluated_seconds": len(values) * FRAME / RATE,
                    }
                    if kind == "positive":
                        record["row"] = {"source": record["source"], "kind": kind}
                        record["positive_frame_mask"] = frame_mask(len(values), interval)
                    added[split][kind].append(record)
                    provenance.append(meta)
            if (index + 1) % 10 == 0 or index + 1 == len(rows):
                print(f"Речевой контекст {split}: {index + 1}/{len(rows)}", flush=True)
        for kind in ("positive", "negative"):
            features[split][kind].extend(added[split][kind])
    (output / "context_augmentation.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2)
    )
    return added
