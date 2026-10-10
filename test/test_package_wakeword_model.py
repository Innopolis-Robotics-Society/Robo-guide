"""Packaging не меняет веса и отказывается перезаписывать другой bundle."""

import hashlib
import json

import pytest

from scripts.package_wakeword_model import package


def test_package_checks_source_and_is_idempotent(tmp_path):
    reference = tmp_path / "session"
    (reference / "model").mkdir(parents=True)
    backbone = tmp_path / "backbone"
    backbone.mkdir()
    hashes = {}
    for name, directory in [
        ("firaya_trial.onnx", reference / "model"),
        ("melspectrogram.onnx", backbone),
        ("embedding_model.onnx", backbone),
    ]:
        (directory / name).write_bytes(name.encode())
        hashes[name] = hashlib.sha256(name.encode()).hexdigest()
    (reference / "session.json").write_text(
        json.dumps(
            {
                "inference_config": {"model": "firaya_trial.onnx"},
                "model_sha256": hashes.pop("firaya_trial.onnx"),
                "backbone_sha256": hashes,
            }
        )
    )
    output = tmp_path / "bundle"
    manifest = package(reference, backbone, output)
    assert package(reference, backbone, output) == manifest
    assert (output / "firaya_trial.onnx").read_bytes() == b"firaya_trial.onnx"
    (output / "firaya_trial.onnx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA256"):
        package(reference, backbone, output)
