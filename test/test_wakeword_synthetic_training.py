from __future__ import annotations

import hashlib
import json
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import generate_wakeword_silero as silero
from scripts import wakeword_synthetic_training as synth


def fixture_dataset(root):
    plan = silero.make_plan("pilot", ["ru_eduard"], "a")
    plan["model_sha256"] = "abc"
    plan["samples"] = plan["samples"][:1]
    sample = plan["samples"][0]
    target = root / sample["file"]
    target.parent.mkdir(parents=True)
    with wave.open(str(target), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(np.ones(16000, dtype="<i2").tobytes())
    row = {
        "sample": sample,
        "model_sha256": "abc",
        "split": "train_only",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    (root / "plan.json").write_text(json.dumps(plan))
    (root / "manifest.jsonl").write_text(json.dumps(row) + "\n")
    return plan, row, target


def test_manifest_integrity_and_correct_stress(tmp_path):
    fixture_dataset(tmp_path)
    plan, rows = synth.load_synthetic(tmp_path)
    assert rows[0]["sample"]["stress"] == "a"
    assert plan["split"] == "train_only"


@pytest.mark.parametrize("problem", ["duplicate", "hash", "stress", "path", "review"])
def test_bad_synthetic_input_rejected(tmp_path, problem):
    plan, row, target = fixture_dataset(tmp_path)
    if problem == "duplicate":
        (tmp_path / "manifest.jsonl").write_text((json.dumps(row) + "\n") * 2)
    elif problem == "hash":
        target.write_bytes(b"broken")
    elif problem == "review":
        plan["mode"] = "pronunciation"
        (tmp_path / "plan.json").write_text(json.dumps(plan))
    else:
        if problem == "stress":
            row["sample"]["stress"] = "i"
        else:
            row["sample"]["file"] = "../escape.wav"
        plan["samples"] = [row["sample"]]
        (tmp_path / "plan.json").write_text(json.dumps(plan))
        (tmp_path / "manifest.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        synth.load_synthetic(tmp_path)


def test_all_utterance_descendants_stay_in_same_split():
    plan = silero.make_plan("pilot", list(silero.SPEAKERS), "a")
    groups = {}
    for sample in plan["samples"]:
        groups.setdefault(sample["text"], set()).add(synth.split_for_sample(sample))
        if sample["kind"] == "positive":
            assert synth.split_for_sample(sample) == "train"
    assert all(len(splits) == 1 for splits in groups.values())
    assert sum(synth.split_for_sample(s) == "val" for s in plan["samples"]) == 48


def test_hard_negative_explicit_splits_override_legacy_filename_rule():
    plan = silero.make_plan("hard_negatives", list(silero.SPEAKERS), "a")
    assert sum(synth.split_for_sample(s) == "val" for s in plan["samples"]) == 216
    grouped = {}
    for sample in plan["samples"]:
        grouped.setdefault(sample["text"], set()).add(synth.split_for_sample(sample))
    assert all(len(splits) == 1 for splits in grouped.values())


@pytest.mark.parametrize("variant", synth.VARIANTS)
def test_augmentation_deterministic_pcm_headroom_and_duration(variant):
    rng = np.random.default_rng(7)
    audio = (rng.normal(size=16000) * 6000).astype("<i2")
    background = (rng.normal(size=160000) * 3000).astype("<i2")
    a, details = synth.augment_stream(audio, background, variant, 123)
    b, repeated = synth.augment_stream(audio, background, variant, 123)
    assert np.array_equal(a, b) and details == repeated
    assert a.dtype == np.dtype("<i2")
    assert len(a) == 16000 * (2 + 1 + 1.28)
    assert np.max(np.abs(a.astype(np.int32))) < 32700
    assert np.any(a[:32000]) and np.any(a[-16000:])


def test_empty_speech_and_short_background_rejected():
    with pytest.raises(ValueError):
        synth.augment_stream(np.ones(16000), np.ones(10), "clean", 1)
    with pytest.raises(ValueError):
        synth.augment_stream(np.zeros(16000), np.ones(160000), "clean", 1)


def test_adding_features_keeps_test_untouched_and_backgrounds_split_local(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "openwakeword.utils", SimpleNamespace(AudioFeatures=lambda **kwargs: object())
    )
    output = tmp_path / "output"
    (output / "features").mkdir(parents=True)
    monkeypatch.setattr(
        synth,
        "read_wav",
        lambda path: np.full(
            160000,
            12 if path.name == "train.wav" else 34 if path.name == "val.wav" else 5,
            dtype=np.int16,
        ),
    )
    backgrounds_seen = []

    def augment(audio, background, variant, seed):
        backgrounds_seen.append(int(background[0]))
        return np.zeros(16000), {"seed": seed}

    monkeypatch.setattr(synth, "augment_stream", augment)
    monkeypatch.setattr(synth, "stream_features", lambda *args: np.zeros((3, 16, 96)))
    rows = [
        {"sample": {"id": str(i), "file": path, "kind": kind}, "sha256": "abc"}
        for i, (path, kind) in enumerate(
            [
                ("positive/ru_eduard/a_positive_01_100.wav", "positive"),
                ("negative/ru_eduard/negative_01_100.wav", "negative"),
                ("negative/ru_eduard/negative_06_100.wav", "negative"),
            ]
        )
    ]
    features = {split: {"positive": [], "negative": []} for split in ("train", "val", "test")}
    plan = {"train": {"negative_sources": ["train.wav"]}, "val": {"negative_sources": ["val.wav"]}}
    added = synth.add_synthetic(features, rows, tmp_path, tmp_path, plan, output, tmp_path, 7)
    assert backgrounds_seen == [12] * 6 + [34] * 3
    assert len(added["train"]["positive"]) == 3
    assert len(added["train"]["negative"]) == 3
    assert len(added["val"]["negative"]) == 3
    assert features["test"] == {"positive": [], "negative": []}
    assert all(row["whole_bag"] for row in added["train"]["negative"])
