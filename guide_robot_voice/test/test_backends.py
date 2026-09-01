"""Юниты на фабрику TTS-бэкендов без модели."""

from __future__ import annotations

import pytest

from guide_robot_voice.lib.backends import SileroBackend, make_backend


def test_make_backend_silero_without_load() -> None:
    backend = make_backend("silero", model_path="/no/such.pt", speaker="xenia")
    assert backend.sample_rate == 48000
    with pytest.raises(RuntimeError, match="load"):
        next(backend.synthesize("привет"))


def test_silero_load_missing_file() -> None:
    backend = SileroBackend(model_path="/no/such.pt")
    with pytest.raises(FileNotFoundError, match="v5_ru"):
        backend.load()
