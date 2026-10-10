"""Train-only Silero augmentation and auxiliary validation for the KWS prototype.

All descendants of a synthesized utterance stay in one split. Auxiliary validation
holds out complete negative texts, not random crops. It is NOT an independent real
test: the TTS voices and engine are shared with training.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import PREFIX, RATE, TAIL, read_wav, stream_features
except ImportError:
    from train_wakeword_trial import PREFIX, RATE, TAIL, read_wav, stream_features

# Keep the five observed false-trigger words in training; hold out four other
# entire texts (all speakers/rates) for auxiliary threshold selection.
VALIDATION_TEXTS = {"negative_06", "negative_09", "negative_12", "negative_16"}
VARIANTS = ("clean", "crowd_12db", "crowd_3db")


def load_synthetic(root: Path) -> tuple[dict, list[dict]]:
    """Загрузить и проверить план синтеза и существующие WAV-примеры."""
    plan = json.loads((root / "plan.json").read_text())
    if (
        plan.get("provider") != "silero"
        or plan.get("mode") not in {"pilot", "hard_negatives", "continuous"}
        or plan.get("split") != "train_only"
    ):
        raise ValueError("нужен Silero pilot train_only, не pronunciation review")
    rows = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
    planned = {sample["id"]: sample for sample in plan["samples"]}
    if len(rows) != len(planned) or len({row["sample"]["id"] for row in rows}) != len(rows):
        raise ValueError("неполный или повторяющийся synthetic manifest")
    for row in rows:
        sample = row["sample"]
        relative = Path(sample["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("недопустимый synthetic path")
        if sample != planned.get(sample["id"]) or row["model_sha256"] != plan["model_sha256"]:
            raise ValueError("synthetic manifest не совпадает с планом/весом")
        if sample["kind"] not in {"positive", "negative"}:
            raise ValueError("неизвестная метка")
        if plan["mode"] in {"hard_negatives", "continuous"} and (
            sample.get("training_split") not in {"train", "val"}
            or plan["mode"] == "hard_negatives"
            and sample["kind"] == "positive"
            and sample["training_split"] != "train"
        ):
            raise ValueError("недопустимый synthetic split")
        if sample["kind"] == "positive" and sample["stress"] != "a":
            raise ValueError("ожидается подтверждённое ударение ФирАя")
        if (
            row["split"] != "train_only"
            or hashlib.sha256((root / relative).read_bytes()).hexdigest() != row["sha256"]
        ):
            raise ValueError(f"повреждён synthetic WAV: {relative}")
        read_wav(root / relative)  # Strict PCM validation before creating the run.
    return plan, rows


def split_for_sample(sample: dict) -> str:
    """Определить train/val без смешивания отложенных семейств слов."""
    if "training_split" in sample:
        return sample["training_split"]
    stem = Path(sample["file"]).stem.rsplit("_", 1)[0]
    return "val" if sample["kind"] == "negative" and stem in VALIDATION_TEXTS else "train"


def augment_stream(audio: np.ndarray, background: np.ndarray, variant: str, seed: int):
    """Apply the SAME transformations to both labels; no validation/test BG in train.

    Modest random gain, sparse artificial echoes, and real microphone background.
    Artificial echoes are a rough augmentation, not a measured XVF room response.
    SNR uses active speech samples; headroom scaling is joint, preserving SNR.
    """
    if variant not in VARIANTS or not len(audio):
        raise ValueError("неизвестный вариант или пустое аудио")
    rng = np.random.default_rng(seed)
    speech = audio.astype(np.float64) / 32768
    if variant != "clean":
        dry = speech.copy()
        for seconds, gain in ((0.03, 0.25), (0.065, 0.12), (0.11, 0.06)):
            delay = round(seconds * RATE)
            if delay < len(speech):
                speech[delay:] += gain * dry[:-delay]
    speech *= 10 ** (rng.uniform(-12, -3) / 20)
    count = round(PREFIX * RATE) + len(speech) + round(TAIL * RATE)
    if len(background) <= count:
        raise ValueError("фоновый исходник слишком короткий")
    offset = int(rng.integers(0, len(background) - count))
    noise = background[offset : offset + count].astype(np.float64) / 32768
    peak = np.max(np.abs(speech))
    active = speech[np.abs(speech) > max(peak * 0.08, 1e-6)]
    if not len(active) or np.sqrt(np.mean(noise**2)) < 1e-7:
        raise ValueError("тишина вместо речи или фона")
    snr = {"clean": 40, "crowd_12db": 12, "crowd_3db": 3}[variant]
    noise *= np.sqrt(np.mean(active**2)) / (np.sqrt(np.mean(noise**2)) * 10 ** (snr / 20))
    start = round(PREFIX * RATE)
    stream = noise.copy()
    stream[start : start + len(speech)] += speech
    divisor = max(1, np.max(np.abs(stream)) / 0.95)
    pcm = np.rint(stream / divisor * 32767).astype("<i2")
    return pcm, {
        "background_offset": offset,
        "snr_db": snr,
        "joint_headroom_scale": 1 / divisor,
        "seed": seed,
        "artificial_echoes": variant != "clean",
    }


def add_synthetic(
    features: dict,
    rows: list[dict],
    root: Path,
    raw: Path,
    real_plan: dict,
    output: Path,
    backbone: Path,
    seed: int,
    reuse: Path | None = None,
) -> dict:
    """Append train bags / held-out-text val negatives; never add anything to test."""
    from openwakeword.utils import AudioFeatures

    extractor = AudioFeatures(
        melspec_model_path=str(backbone / "melspectrogram.onnx"),
        embedding_model_path=str(backbone / "embedding_model.onnx"),
        inference_framework="onnx",
        ncpu=1,
    )
    backgrounds = {
        split: np.concatenate(
            [read_wav(raw / source) for source in real_plan[split]["negative_sources"]]
        )
        for split in ("train", "val")
    }
    added = {split: {"positive": [], "negative": []} for split in ("train", "val")}
    provenance = []
    previous = {}
    if reuse:
        old_provenance = json.loads((reuse / "feature_provenance.json").read_text())
        if (
            json.loads((reuse / "split_plan.json").read_text()) != real_plan
            or old_provenance["seed"] != seed
            or old_provenance["backbone"] != json.loads((backbone / "provenance.json").read_text())
        ):
            raise ValueError("несовместимый synthetic feature cache")
        previous = {
            item["source"]: item
            for item in json.loads((reuse / "synthetic_augmentation.json").read_text())
        }
    for index, row in enumerate(rows):
        sample = row["sample"]
        split = split_for_sample(sample)
        audio = read_wav(root / sample["file"])
        for variant in VARIANTS:
            identity = hashlib.sha256(f"{seed}:{sample['id']}:{variant}".encode()).hexdigest()
            stream, details = augment_stream(
                audio, backgrounds[split], variant, int(identity[:8], 16)
            )
            filename = f"synthetic_{identity}.npy"
            cached = reuse / "features" / filename if reuse else None
            # Reuse only with identical source SHA, split-local BG, seed and transform.
            # Shape/finite checks protect accidental corruption; manifest below is the
            # authoritative metadata and includes every augmentation parameter.
            expected = {
                "split": split,
                "source": f"silero/{sample['file']}#{variant}",
                "source_sha256": row["sha256"],
                "variant": variant,
                "background_sources": real_plan[split]["negative_sources"],
                **details,
            }
            reused = cached and cached.is_file() and previous.get(expected["source"]) == expected
            if reused:
                value = np.load(cached, allow_pickle=False)
                if (
                    value.ndim != 3
                    or value.shape[1:] != (16, 96)
                    or not np.all(np.isfinite(value))
                ):
                    raise ValueError("повреждён synthetic feature cache")
            else:
                value = stream_features(extractor, stream, PREFIX)
            if reused:
                (output / "features" / filename).hardlink_to(cached)
            else:
                np.save(output / "features" / filename, value)
            source = f"silero/{sample['file']}#{variant}"
            if sample["kind"] == "positive":
                record = {
                    "row": {"source": source, "kind": "positive"},
                    "features": value,
                    "source_seconds": len(audio) / RATE,
                }
            else:
                record = {
                    "source": source,
                    "features": value,
                    "seconds": len(stream) / RATE,
                    "evaluated_seconds": (len(stream) / RATE - PREFIX),
                    "whole_bag": True,
                }
            added[split][sample["kind"]].append(record)
            provenance.append(
                {
                    "split": split,
                    "source": source,
                    "source_sha256": row["sha256"],
                    "variant": variant,
                    "background_sources": real_plan[split]["negative_sources"],
                    **details,
                }
            )
        if (index + 1) % 20 == 0 or index + 1 == len(rows):
            print(f"Silero признаки: {index + 1}/{len(rows)}", flush=True)
    for split, groups in added.items():
        for kind, records in groups.items():
            features[split][kind].extend(records)
    (output / "synthetic_augmentation.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2)
    )
    return added
