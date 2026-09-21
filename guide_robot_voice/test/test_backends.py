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


def test_silero_rejects_device_rate_as_synthesis_rate() -> None:
    """XVF3800 16кГц -- частота устройства, не допустимый output Silero v5."""
    with pytest.raises(ValueError, match="не поддерживает 16000"):
        SileroBackend(model_path="/no/such.pt", sample_rate=16000)
