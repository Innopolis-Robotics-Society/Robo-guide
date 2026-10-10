#!/usr/bin/env python3
"""Зафиксировать понравившуюся live-модель в deploy bundle без переобучения."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "datasets/wakeword/firaya_v1"
DEFAULT_REFERENCE = DATA / "evaluation/live_20261007_110955_UTC_4e2839"
DEFAULT_OUTPUT = ROOT / "guide_robot_voice/models/firaya"


def package(reference: Path, backbone: Path, output: Path) -> dict:
    """Копировать проверенные ONNX; существующий bundle никогда не перезаписывать."""
    session = json.loads((reference / "session.json").read_text(encoding="utf-8"))
    config = session["inference_config"]
    if config["model"] != "firaya_trial.onnx" or config.get("feature_kind") is not None:
        raise ValueError("Нужна embedding-модель firaya_trial.onnx")
    files = {
        "firaya_trial.onnx": reference / "model/firaya_trial.onnx",
        "melspectrogram.onnx": backbone / "melspectrogram.onnx",
        "embedding_model.onnx": backbone / "embedding_model.onnx",
    }
    hashes = {"firaya_trial.onnx": session["model_sha256"], **session["backbone_sha256"]}
    for name, source in files.items():
        if hashlib.sha256(source.read_bytes()).hexdigest() != hashes[name]:
            raise ValueError(f"SHA256 mismatch: {source}")
    manifest = {
        "schema_version": 1,
        "runtime": "openwakeword==0.6.0",
        "source_session": reference.name,
        "inference_config": config,
        "sha256": hashes,
        "status": "bench_candidate_not_production_validated",
    }
    if output.exists():
        existing = json.loads((output / "bundle.json").read_text(encoding="utf-8"))
        if existing != manifest:
            raise FileExistsError(f"Другой bundle уже существует: {output}")
        for name in files:
            if hashlib.sha256((output / name).read_bytes()).hexdigest() != hashes[name]:
                raise ValueError(f"SHA256 mismatch в существующем bundle: {name}")
        return manifest
    output.mkdir(parents=True)
    for name, source in files.items():
        shutil.copyfile(source, output / name)
    with (output / "bundle.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


def main(argv=None) -> int:
    """Создать deploy bundle из frozen session, не из изменяемого experiment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-session", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    manifest = package(args.reference_session, args.backbone, args.output)
    print(f"Deploy bundle: {args.output}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
