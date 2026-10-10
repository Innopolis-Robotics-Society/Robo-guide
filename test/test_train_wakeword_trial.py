from __future__ import annotations

import json

import numpy as np
import pytest

from scripts import train_wakeword_trial as trial


def rows_for_split():
    rows = [
        {"kind": "positive", "source": f"positive/train/s{index:02d}/d01/a.wav"}
        for index in range(1, 5)
    ]
    rows.extend(
        {"kind": "negative", "source": f"negative/train/d01/crowd_{index:02d}.wav"}
        for index in range(1, 4)
        for _ in range(10)
    )
    return rows


def test_splits_hold_out_whole_people_and_whole_negative_sources():
    plan = trial.plan_split(rows_for_split(), "s03", "s04")
    assert len(plan["train"]["positive"]) == 2
    assert plan["val"]["positive"][0]["source"].split("/")[2] == "s03"
    assert plan["test"]["positive"][0]["source"].split("/")[2] == "s04"
    assert all(len(part["negative_sources"]) == 1 for part in plan.values())
    sources = [part["negative_sources"][0] for part in plan.values()]
    assert len(set(sources)) == 3


def test_invalid_speaker_split_rejected():
    with pytest.raises(ValueError):
        trial.plan_split(rows_for_split(), "s03", "s03")
    with pytest.raises(ValueError):
        trial.plan_split(rows_for_split(), "s08", "s04")
    with pytest.raises(ValueError):
        trial.plan_split(rows_for_split()[:4], "s03", "s04")


def test_detection_requires_two_consecutive_frames():
    assert not trial.event_indices(np.array([0.1, 0.9, 0.1]), 0.5)
    assert trial.max_confirmed_score(np.array([0.1, 0.9, 0.1])) == 0.1
    assert trial.event_indices(np.array([0.1, 0.9, 0.8]), 0.5) == [2]
    assert trial.max_confirmed_score(np.array([0.1, 0.9, 0.8])) == 0.8


def test_confirmation_length_is_configurable_without_accepting_short_spikes():
    scores = np.array([0.1, 0.9, 0.9, 0.1, 0.8, 0.8, 0.8, 0.8])
    assert trial.event_indices(scores, 0.5, consecutive_frames=4) == [7]
    assert trial.max_confirmed_score(scores, 4) == 0.8
    assert trial.max_confirmed_score(scores, 9) == 0
    with pytest.raises(ValueError):
        trial.max_confirmed_score(scores, 0)
    with pytest.raises(ValueError):
        trial.event_indices(scores, 0.5, consecutive_frames=0)


def test_sustained_false_activation_not_hidden_by_single_episode():
    events = trial.event_indices(np.ones(100), 0.5)
    assert events == [1, 26, 51, 76]


def test_validation_threshold_prefers_zero_false_activations():
    positives = [np.array([0.1, 0.9, 0.9]), np.array([0.1, 0.55, 0.55])]
    negatives = [np.array([0.1, 0.6, 0.6])]
    selected = trial.choose_threshold(positives, negatives)
    assert selected["threshold"] > 0.6
    assert selected["false_events"] == 0
    assert selected["hits"] == 1
    assert not trial.event_indices(negatives[0], selected["threshold"])
    assert json.loads(json.dumps(selected)) == selected


def test_threshold_includes_numerical_margin_above_float32_peak():
    negatives = [np.array([0.913, 0.913], dtype=np.float32)]
    positives = [np.array([0.96, 0.96], dtype=np.float32)]
    selected = trial.choose_threshold(positives, negatives)
    assert selected["threshold"] - trial.max_confirmed_score(negatives[0]) >= 0.0049
    assert selected["hits"] == 1 and selected["false_events"] == 0


def test_stream_features_uses_warmup_and_pads_last_chunk():
    class Extractor:
        def reset(self):
            self.calls = 0

        def __call__(self, chunk):
            assert len(chunk) == trial.FRAME
            self.calls += 1

        def get_features(self, _):
            return np.full((1, 16, 96), self.calls, dtype=np.float32)

    actual = trial.stream_features(Extractor(), np.zeros(32001, dtype=np.int16), 2)
    assert actual.shape == (1, 16, 96)
    assert actual[0, 0, 0] == 26


def test_reusing_reused_cache_follows_provenance(tmp_path):
    original = tmp_path / "original"
    (original / "features").mkdir(parents=True)
    np.save(original / "features/train.npy", np.zeros((1, 16, 96), dtype=np.float32))
    reused = tmp_path / "reused"
    (reused / "features").mkdir(parents=True)
    np.save(reused / "features/synthetic_only.npy", np.zeros((1, 16, 96), dtype=np.float32))
    (reused / "feature_provenance.json").write_text(json.dumps({"reused_from": str(original)}))
    assert trial.resolve_feature_cache(reused) == original.resolve()


def test_cache_cycles_and_missing_cache_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        trial.resolve_feature_cache(tmp_path)
    (tmp_path / "feature_provenance.json").write_text(json.dumps({"reused_from": str(tmp_path)}))
    with pytest.raises(ValueError):
        trial.resolve_feature_cache(tmp_path)


@pytest.mark.parametrize("positive,negative", [(3, 40), (20, 17), (1, 1)])
def test_balanced_batches_cover_negatives_and_replay_positive_examples(positive, negative):
    order = trial.balanced_bag_order(positive, negative, np.random.default_rng(1))
    assert len(order) == negative * 2
    assert sorted(order[order >= positive]) == list(range(positive, positive + negative))
    for start in range(0, len(order), 16):
        batch = order[start : start + 16]
        assert sum(batch < positive) == sum(batch >= positive)
    assert np.array_equal(
        order, trial.balanced_bag_order(positive, negative, np.random.default_rng(1))
    )


@pytest.mark.parametrize("positive,negative", [(0, 1), (1, 0)])
def test_balancing_rejects_missing_classes(positive, negative):
    with pytest.raises(ValueError):
        trial.balanced_bag_order(positive, negative, np.random.default_rng(1))


def test_finetuning_reuses_normalization_and_evaluates_initial_weights(tmp_path):
    torch = pytest.importorskip("torch")
    initial = tmp_path / "initial"
    initial.mkdir()
    mean = np.full(96, 0.2, dtype=np.float32)
    std = np.full(96, 0.4, dtype=np.float32)
    model = trial.build_model(mean, std)
    torch.save(model.state_dict(), initial / "firaya_trial.pt")
    feature = np.ones((3, 16, 96), dtype=np.float32)
    groups = {
        "positive": [{"features": feature}],
        "negative": [{"features": feature * 0.5, "whole_bag": True}],
    }
    output = tmp_path / "run"
    output.mkdir()
    trained, selected = trial.train_head(
        {"train": groups, "val": groups}, output, 1, 7, initial, 0.0001, True
    )
    assert np.array_equal(trained.mean.numpy(), mean)
    assert np.array_equal(trained.std.numpy(), std)
    history = json.loads((output / "training_history.json").read_text())
    assert [row["epoch"] for row in history] == [0, 1]
    assert selected["epoch"] in (0, 1)


def test_temporal_head_has_same_streaming_input_and_gradient():
    torch = pytest.importorskip("torch")
    model = trial.build_model(
        np.zeros(96, dtype=np.float32), np.ones(96, dtype=np.float32), 48, True, "tcn"
    )
    inputs = torch.randn(3, 16, 96, requires_grad=True)
    logits = model(inputs)
    assert logits.shape == (3, 1)
    logits.sum().backward()
    assert torch.isfinite(inputs.grad).all()
    with pytest.raises(ValueError):
        trial.build_model(
            np.zeros(96, dtype=np.float32), np.ones(96, dtype=np.float32), 33, True, "tcn"
        )
