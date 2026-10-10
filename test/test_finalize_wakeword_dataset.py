from __future__ import annotations

import copy
import hashlib
import json
import wave

import pytest

from scripts import finalize_wakeword_dataset as finalizer
from scripts.wakeword_review import dataset_fingerprint


def fixture_dataset(tmp_path):
    raw, run = tmp_path / "raw", tmp_path / "prepared/run_02"
    rows = []
    for kind, prefix, name in (
        ("positive", "positive_candidates", "quiet.wav"),
        ("positive", "positive_candidates", "crowd.wav"),
        ("negative", "negative_pending", "background.wav"),
    ):
        source = raw / kind / "train" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x01\x00" * 16000)
        candidate = run / prefix / "train" / name
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(source.read_bytes())
        rows.append(
            {
                "source": str(source.relative_to(raw)),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "candidate": str(candidate.relative_to(run)),
                "kind": kind,
                "split": "train",
                "crop_start_sample": 0,
                "crop_end_sample": 16000,
                "label_verified": False,
                "review_required": True,
            }
        )
    (run / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    payload = {
        "schema_version": 1,
        "dataset_fingerprint": dataset_fingerprint(rows),
        "decisions": {
            rows[1]["candidate"]: {
                "choice": "rejected",
                "note": "не произносили имя",
                "checked_at": "2026-10-06T12:39:54.603Z",
            }
        },
    }
    decisions = tmp_path / "review.json"
    decisions.write_text(json.dumps(payload))
    return raw, run, rows, payload, decisions


def test_default_never_implicitly_accepts_unreviewed_rows(tmp_path):
    _, _, rows, payload, _ = fixture_dataset(tmp_path)
    selected, excluded = finalizer.select_rows(rows, payload["decisions"], False, False)
    assert not selected
    assert len(excluded) == 3
    assert {row["exclusion_reason"] for row in excluded} == {"not_reviewed", "manual_rejection"}


def test_explicit_policy_excludes_rejections_without_relabelling(tmp_path):
    raw, run, rows, _, decisions = fixture_dataset(tmp_path)
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "curated/reviewed_01"
    summary = finalizer.finalize(run, raw, decisions, output, True, True)
    included = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    excluded = [json.loads(line) for line in (output / "excluded.jsonl").read_text().splitlines()]
    assert summary["included"] == {"positive": 1, "negative": 1}
    assert summary["review_stage_closed"] and not summary["training_ready"]
    assert excluded[0]["kind"] == "positive" and not excluded[0]["included"]
    assert not (output / "positive/train/crowd.wav").exists()
    assert not (output / "negative/train/crowd.wav").exists()
    positive = next(row for row in included if row["kind"] == "positive")
    negative = next(row for row in included if row["kind"] == "negative")
    assert not positive["boundary_verified_by_human"] and not positive["label_verified"]
    assert positive["inclusion_basis"] == "user_approved_automatic_crop"
    assert negative["label_verified"] and not negative["individually_reviewed"]
    for row in included:
        original = (run / row["prepared_candidate"]).read_bytes()
        assert (output / row["file"]).read_bytes() == original
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items()
    )
    with pytest.raises(FileExistsError):
        finalizer.finalize(run, raw, decisions, output, True, True)


def test_accepted_row_is_individually_reviewed(tmp_path):
    _, _, rows, payload, _ = fixture_dataset(tmp_path)
    payload["decisions"][rows[0]["candidate"]] = {
        "choice": "accepted",
        "note": "",
        "checked_at": "2026-10-06T12:39:54.603Z",
    }
    included, _ = finalizer.select_rows(rows, payload["decisions"], False, False)
    assert len(included) == 1
    assert included[0]["label_verified"] and included[0]["boundary_verified_by_human"]
    assert included[0]["individually_reviewed"]


@pytest.mark.parametrize("change", ["fingerprint", "unknown", "choice", "timestamp"])
def test_wrong_decisions_rejected_before_any_writes(tmp_path, change):
    raw, run, rows, original, decisions = fixture_dataset(tmp_path)
    payload = copy.deepcopy(original)
    if change == "fingerprint":
        payload["dataset_fingerprint"] = "wrong"
    elif change == "unknown":
        payload["decisions"]["unknown.wav"] = next(iter(payload["decisions"].values()))
    else:
        item = payload["decisions"][rows[1]["candidate"]]
        item["choice" if change == "choice" else "checked_at"] = "invalid"
    decisions.write_text(json.dumps(payload))
    output = tmp_path / "curated/reviewed_01"
    with pytest.raises(ValueError):
        finalizer.finalize(run, raw, decisions, output, True, True)
    assert not output.exists()


@pytest.mark.parametrize("changed", ["source", "candidate"])
def test_changed_audio_is_rejected(tmp_path, changed):
    raw, run, rows, _, decisions = fixture_dataset(tmp_path)
    path = (raw / rows[0]["source"]) if changed == "source" else (run / rows[0]["candidate"])
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    output = tmp_path / "curated/reviewed_01"
    with pytest.raises(ValueError):
        finalizer.finalize(run, raw, decisions, output, True, True)
    assert not output.exists()


def test_paths_cannot_escape_dataset(tmp_path):
    with pytest.raises(ValueError):
        finalizer.inside(tmp_path, "../outside.wav")
