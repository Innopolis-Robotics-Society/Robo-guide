"""PCM framing и frozen deploy bundle без ROS/обучающих зависимостей."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from guide_robot_voice.lib.wakeword_runtime import (
    FILES,
    FRAME,
    FrozenWakewordModel,
    WakewordStream,
    validate_bundle,
)

CONFIG = {
    "model": "firaya_trial.onnx",
    "threshold": 0.8,
    "consecutive_frames": 3,
    "cooldown_seconds": 2,
    "feature_frames": 16,
    "feature_dim": 96,
}


class Model:
    def __init__(self, scores=None):
        self.scores = scores
        self.frames = []
        self.resets = 0

    def reset(self):
        self.frames = []
        self.resets += 1

    def score(self, pcm):
        self.frames.append(pcm.copy())
        return self.scores[len(self.frames) - 1] if self.scores is not None else 0.9


@pytest.mark.parametrize("chunk_size", [256, 512, 1280, 2048, 777])
def test_framing_never_drops_or_duplicates_pcm(chunk_size):
    model = Model()
    stream = WakewordStream(model, CONFIG)
    audio = np.arange(8000, dtype=np.int16)
    events = []
    for first in range(0, len(audio), chunk_size):
        events.extend(
            stream.push("a", first, 10 + first / 16000, audio[first : first + chunk_size])
        )
    np.testing.assert_array_equal(np.concatenate(model.frames), audio[:7680])
    np.testing.assert_array_equal(stream.pending, audio[7680:])
    assert len(events) == 1
    assert events[0].end_sample == 3840
    assert events[0].timestamp == pytest.approx(10.24)


def test_cooldown_and_consecutive_scores_match_evaluation():
    scores = [0.9, 0.9, 0.1, 0.9, 0.9, 0.9] + [0.9] * 30
    stream = WakewordStream(Model(scores), CONFIG)
    events = stream.push("a", 0, 0, np.zeros(len(scores) * FRAME, dtype=np.int16))
    assert [event.end_sample // FRAME - 1 for event in events] == [5, 30]


@pytest.mark.parametrize("session,first", [("a", 5000), ("b", 2560), ("a", 0)])
def test_gap_reconnect_and_duplicate_reset_confirmation(session, first):
    model = Model()
    stream = WakewordStream(model, CONFIG)
    assert stream.push("a", 0, 0, np.ones(FRAME * 2 + 100, dtype=np.int16)) == []
    assert stream.push(session, first, 1, np.ones(FRAME, dtype=np.int16)) == []
    assert stream.discontinuities == 1
    assert stream.pending.size == 0
    events = stream.push(session, first + FRAME, 1.08, np.ones(FRAME * 2, dtype=np.int16))
    assert len(events) == 1 and events[0].end_sample == first + 3 * FRAME


def test_invalid_score_is_not_a_detection():
    stream = WakewordStream(Model([float("nan")]), CONFIG)
    with pytest.raises(ValueError, match="score"):
        stream.push("a", 0, 0, np.zeros(FRAME, dtype=np.int16))


def test_cached_reset_matches_real_runtime_and_does_not_run_inference(monkeypatch):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("openwakeword")
    bundle = Path(__file__).resolve().parents[1] / "models" / "firaya"
    model = FrozenWakewordModel(bundle)
    audio = np.random.default_rng(42).integers(-10000, 10000, FRAME * 30, dtype=np.int16)

    def scores():
        return [model.score(audio[first : first + FRAME]) for first in range(0, len(audio), FRAME)]

    # Reference behavior before caching: upstream reset followed by 2 s silence.
    model.model.reset()
    for _ in range(25):
        model.model.predict(np.zeros(FRAME, dtype=np.int16))
    expected = scores()

    model.reset()  # Build the reusable silence state once.
    np.testing.assert_allclose(scores(), expected, atol=1e-6, rtol=0)

    def unexpected_inference(*args, **kwargs):
        raise AssertionError("reset must restore buffers without upstream reset/predict")

    # Repeated use must not retain audio or mutate the reusable silence state.
    for _ in range(3):
        with monkeypatch.context() as patch:
            patch.setattr(model.model, "reset", unexpected_inference)
            patch.setattr(model.model, "predict", unexpected_inference)
            model.reset()
        np.testing.assert_allclose(scores(), expected, atol=1e-6, rtol=0)


def test_bundle_hash_and_config_checked_before_loading_runtime(tmp_path):
    hashes = {}
    for name in FILES:
        (tmp_path / name).write_bytes(name.encode())
        hashes[name] = hashlib.sha256(name.encode()).hexdigest()
    manifest = {"runtime": "openwakeword==0.6.0", "inference_config": CONFIG, "sha256": hashes}
    (tmp_path / "bundle.json").write_text(json.dumps(manifest))
    assert validate_bundle(tmp_path) == manifest
    (tmp_path / FILES[1]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="SHA256"):
        validate_bundle(tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"threshold": float("nan")},
        {"consecutive_frames": 0},
        {"cooldown_seconds": float("inf")},
        {"feature_frames": 15},
    ],
)
def test_invalid_config_rejected(tmp_path, changes):
    (tmp_path / "bundle.json").write_text(
        json.dumps(
            {
                "runtime": "openwakeword==0.6.0",
                "inference_config": {**CONFIG, **changes},
            }
        )
    )
    with pytest.raises(ValueError):
        validate_bundle(tmp_path)
