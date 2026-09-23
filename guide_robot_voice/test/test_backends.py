"""Юниты на фабрику TTS-бэкендов без модели."""

from __future__ import annotations

import numpy as np
import pytest

from guide_robot_voice.lib.backends import (
    SileroBackend,
    build_silero_ssml,
    make_backend,
    trim_trailing_silence,
)


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
def test_ssml_not_built_for_defaults() -> None:
    """При темпе 100% и модельной паузе apply_tts зовётся с text, не ssml."""
    assert build_silero_ssml("Привет. Как дела?") is None
    assert build_silero_ssml("Привет.", rate=" 100% ", sentence_pause_ms=0) is None


def test_ssml_rate_and_breaks_with_escaping() -> None:
    """Предложения разделены <break>, темп обёрнут в <prosody>, спецсимволы экранированы."""
    ssml = build_silero_ssml("Привет! Тут <5 & больше. Ясно?", rate="120%", sentence_pause_ms=150)
    assert ssml == (
        '<speak><prosody rate="120%">Привет!<break time="150ms"/>'
        'Тут &lt;5 &amp; больше.<break time="150ms"/>Ясно?</prosody></speak>'
    )


def test_ssml_rate_only_keeps_text_whole() -> None:
    """Без sentence_pause_ms предложения не режутся, модельная пауза остаётся."""
    assert (
        build_silero_ssml("А. Б.", rate="fast")
        == '<speak><prosody rate="fast">А. Б.</prosody></speak>'
    )


def test_trim_trailing_silence() -> None:
    """Хвост тише 200 LSB укорачивается до keep_ms; речь и -1 не трогаются."""
    sr = 1000
    pcm = np.concatenate([np.full(500, 3000, np.int16), np.full(500, 5, np.int16)])
    assert trim_trailing_silence(pcm, 100, sr).size == 600
    assert trim_trailing_silence(pcm, 0, sr).size == 500
    assert trim_trailing_silence(pcm, -1, sr).size == 1000
    assert trim_trailing_silence(pcm, 900, sr).size == 1000  # не длиннее исходного
    quiet = np.full(300, 5, np.int16)
    assert trim_trailing_silence(quiet, 10, sr).size == 300  # сплошная тишина -- как есть
