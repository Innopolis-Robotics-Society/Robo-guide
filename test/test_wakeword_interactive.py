from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import test_wakeword_interactive as runner


def experiment(tmp_path):
    model = tmp_path / "experiment"
    backbone = tmp_path / "backbone"
    model.mkdir()
    backbone.mkdir()
    config = {
        "model": "firaya_trial.onnx",
        "threshold": 0.5,
        "consecutive_frames": 2,
        "cooldown_seconds": 2,
    }
    (model / "inference_config.json").write_text(json.dumps(config))
    (model / "firaya_trial.onnx").write_bytes(b"fake model")
    for name in ("melspectrogram.onnx", "embedding_model.onnx"):
        (backbone / name).write_bytes(name.encode())
    return model, backbone


class Runtime:
    def reset(self):
        pass

    def predict(self, chunk):
        return {"firaya_trial": float(np.max(chunk) > 100)}


def fake_capture(topic, seconds, planned):
    audio = np.zeros(round(seconds * 16000), dtype="<i2")
    for cue in planned:
        for occurrence in range(cue.get("expected_events", int(cue["contains_wakeword"]))):
            start = round((cue["planned_seconds"] + 0.4 + occurrence * 5) * 16000)
            audio[start : start + 6400] = 1000
    return audio.tobytes(), {
        "sample_rate": 16000,
        "channels": 1,
        "duration_seconds": seconds,
        "gaps": 0,
        "interrupted": False,
        "channel_metrics": [],
        "cues": [{**cue, "actual_seconds": cue["planned_seconds"]} for cue in planned],
    }


def setup_run(monkeypatch, tmp_path, capture=fake_capture):
    model, backbone = experiment(tmp_path)
    output = tmp_path / "output"
    monkeypatch.setattr(runner, "load_runtime", lambda *args: Runtime())
    monkeypatch.setattr(runner, "capture", capture)
    monkeypatch.setattr(runner, "wait_enter", lambda text: None)
    args = [
        "--experiment",
        str(model),
        "--backbone",
        str(backbone),
        "--output",
        str(output),
        "--suite",
        "classic",
    ]
    return args, output


def test_protocol_is_exactly_seven_scenarios_twice_without_target_in_negatives():
    assert [test.number for test in runner.TESTS] == list(range(1, 8))
    assert [test.expected for test in runner.TESTS] == [5, 0, 0, 0, 3, 3, 0]
    assert sum(test.expected for test in runner.TESTS) * 2 == 22
    for test in runner.TESTS:
        for text, label in test.utterances:
            assert ("фирая" in text.lower()) == label
        cues = test.cues(20)
        assert len(cues) == len(test.utterances)
        assert all(cue["planned_seconds"] <= 17 for cue in cues)
        assert all(cue["window_end_seconds"] <= 20 for cue in cues)
        assert all(cue["window_end_seconds"] - cue["planned_seconds"] >= 3 for cue in cues)


def test_dry_run_never_loads_model_records_or_creates_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "load_runtime", lambda *args: pytest.fail("model load"))
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("recording"))
    output = tmp_path / "dry"
    assert runner.main(["--dry-run", "--output", str(output)]) == 0
    assert not output.exists()
    assert "ФирАя" in capsys.readouterr().out


def test_extended_protocol_has_context_and_matched_negative_phrases():
    assert [test.number for test in runner.EXTENDED_TESTS] == list(range(1, 12))
    assert [test.expected for test in runner.EXTENDED_TESTS] == [5, 0, 0, 0, 3, 3, 0, 3, 3, 3, 0]
    for test in runner.EXTENDED_TESTS:
        for text, label in test.utterances:
            assert text.lower().count("фирая") == int(label)
        assert all(
            cue["window_end_seconds"] - cue["planned_seconds"] >= 3 for cue in test.cues(20)
        )
    assert all(text.startswith("ФирАя,") for text, _ in runner.EXTENDED_TESTS[7].utterances)
    assert all(text.endswith("ФирАя") for text, _ in runner.EXTENDED_TESTS[9].utterances)


def test_speech_protocol_preserves_classic_and_gives_long_text_time():
    assert runner.SPEECH_TESTS[:7] == runner.TESTS
    assert [test.number for test in runner.SPEECH_TESTS] == list(range(1, 16))
    assert sum(test.expected for test in runner.SPEECH_TESTS) * 2 == 44
    for test in runner.SPEECH_TESTS:
        for text, label in test.utterances:
            assert text.lower().count("фирая") == int(label)
        cues = test.cues(test.seconds)
        assert all(cue["window_end_seconds"] <= test.seconds for cue in cues)
        if test.number >= 8:
            assert all(cue["window_end_seconds"] - cue["planned_seconds"] >= 12 for cue in cues)
    assert all(text.startswith("ФирАя ") for text, _ in runner.SPEECH_TESTS[7].utterances)
    assert all(text.endswith("ФирАя") for text, _ in runner.SPEECH_TESTS[9].utterances)
    assert all(test.continuous and len(test.utterances) == 1 for test in runner.SPEECH_TESTS[11:])
    assert runner.SPEECH_TESTS[14].utterances[0][0] == (
        runner.SPEECH_TESTS[11].utterances[0][0].replace("ФирАя ", "")
    )


def test_speech_run_saves_30_takes_variable_durations_and_resumes(monkeypatch, tmp_path, capsys):
    args, output = setup_run(monkeypatch, tmp_path)
    args = args[:-1] + ["speech"]
    assert runner.main(args) == 0
    summary = json.loads((output / "results.json").read_text())
    assert summary["complete"] and summary["completed_takes"] == summary["planned_takes"] == 30
    assert sum(row["expected_events"] for row in summary["takes"]) == 44
    assert all(row["count_matches"] and row["audio_protocol_valid"] for row in summary["takes"])
    for row in summary["takes"]:
        seconds = 20 if row["test"] <= 7 else 40 if row["test"] <= 11 else 45
        assert row["requested_seconds"] == row["capture"]["duration_seconds"] == seconds
        assert len(runner.read_wav(output / row["file"])) == seconds * runner.RATE
    assert "Сначала прочитай текст глазами" in capsys.readouterr().out
    original = {path: path.read_bytes() for path in (output / "audio").glob("*.wav")}
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not re-record"))
    assert runner.main(args[:-2] + ["--resume"]) == 0
    assert all(path.read_bytes() == data for path, data in original.items())


def test_speech_dry_run_respects_custom_minimum(monkeypatch, capsys):
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not record"))
    assert runner.main(["--suite", "speech", "--seconds", "50", "--dry-run"]) == 0
    output = capsys.readouterr().out
    protocol = json.loads(output[output.index("[\n") :])
    assert len(protocol) == 30
    assert all(task["requested_seconds"] == 50 for task in protocol)
    assert all(task["cues"][-1]["window_end_seconds"] <= 50 for task in protocol)
    assert all(
        task["cues"][-1]["window_end_seconds"] == 50
        for task in protocol
        if task["test"] in (7, 12, 13, 14, 15)
    )


def test_long_protocol_has_whole_text_six_targets_and_matched_negative():
    positive, negative = runner.LONG_TESTS
    assert positive.expected == positive.utterances[0][0].lower().count("фирая") == 6
    assert positive.utterances[0][0].startswith("ФирАя")
    assert positive.utterances[0][0].endswith("ФирАя.")
    assert "Феррари" in positive.utterances[0][0]
    assert negative.expected == 0 and "фирая" not in negative.utterances[0][0].lower()
    assert negative.utterances[0][0] == positive.utterances[0][0].replace("ФирАя", "Фарида")
    (cue,) = positive.cues(positive.seconds)
    assert cue["expected_events"] == 6 and cue["contains_wakeword"] is True
    assert cue["planned_seconds"] == 5 and cue["window_end_seconds"] == 240


def test_multi_target_slot_counts_misses_extras_and_marks_count_only():
    cue = {**runner.LONG_TESTS[0].cues(240)[0], "actual_seconds": 5}
    result = runner.assess_events([1, 8, 20, 40, 60], [cue])
    assert result["estimated_hits"] == 4
    assert result["estimated_misses"] == 2
    assert result["estimated_extra_events"] == 1
    assert result["matching_mode"] == "count_only_multitarget" and not result["label_verified"]
    result = runner.assess_events([8, 20, 40, 60, 80, 100, 120], [cue])
    assert result["estimated_hits"] == 6 and result["estimated_extra_events"] == 1


def test_long_run_uses_frozen_reference_even_after_original_experiment_changes(
    monkeypatch, tmp_path
):
    args, reference = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    (tmp_path / "experiment/firaya_trial.onnx").write_bytes(b"changed experiment")
    output = tmp_path / "long"
    new_args = [
        "--suite",
        "long",
        "--reference-session",
        str(reference),
        "--backbone",
        str(tmp_path / "backbone"),
        "--output",
        str(output),
    ]
    assert runner.main(new_args) == 0
    session = json.loads((output / "session.json").read_text())
    assert session["model_sha256"] == hashlib.sha256(b"fake model").hexdigest()
    assert session["reference_session"] == str(reference)
    assert (output / "model/firaya_trial.onnx").read_bytes() == b"fake model"
    summary = json.loads((output / "results.json").read_text())
    assert summary["complete"] and summary["completed_takes"] == 4
    assert [row["observed_events"] for row in summary["takes"]] == [6, 6, 0, 0]
    assert all(row["requested_seconds"] == 240 for row in summary["takes"])
    assert "Феррари" in (output / "reading.html").read_text()
    before = (output / "reading.html").read_bytes()
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not re-record"))
    assert runner.main(new_args + ["--resume"]) == 0
    assert (output / "reading.html").read_bytes() == before


def test_corrupt_reference_model_is_rejected_not_copied(monkeypatch, tmp_path):
    args, reference = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    (reference / "model/firaya_trial.onnx").write_bytes(b"corrupt frozen copy")
    output = tmp_path / "long"
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not record"))
    assert (
        runner.main(
            [
                "--suite",
                "long",
                "--reference-session",
                str(reference),
                "--backbone",
                str(tmp_path / "backbone"),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not (output / "model/firaya_trial.onnx").exists()


def test_default_extended_run_saves_22_takes_and_resumes_without_suite(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    args = args[:-2]  # New sessions default to extended, old explicit classic still works.
    assert runner.main(args) == 0
    summary = json.loads((output / "results.json").read_text())
    assert summary["complete"] and summary["planned_takes"] == summary["completed_takes"] == 22
    assert len(list((output / "audio").glob("*.wav"))) == 22
    assert len(list((output / "takes").glob("*_scores.json"))) == 22
    assert all(row["count_matches"] for row in summary["takes"])
    assert "22/22" in (output / "review.html").read_text()
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not re-record"))
    assert runner.main(args + ["--resume"]) == 0


def test_legacy_session_without_suite_can_resume(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    path = output / "session.json"
    session = json.loads(path.read_text())
    del session["suite"]
    path.write_text(json.dumps(session))
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must not re-record"))
    assert runner.main(args[:-2] + ["--resume"]) == 0


def test_suite_mismatch_on_resume_keeps_existing_recordings(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    before = {path: path.read_bytes() for path in (output / "audio").glob("*.wav")}
    assert runner.main(args[:-2] + ["--suite", "extended", "--resume"]) == 2
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("value", ["0", "19", "nan", "inf"])
def test_invalid_duration_rejected(value):
    with pytest.raises(SystemExit):
        runner.main(["--dry-run", "--seconds", value])


def test_slot_assessment_catches_equal_count_but_wrong_words():
    cues = [{**cue, "actual_seconds": cue["planned_seconds"]} for cue in runner.TESTS[4].cues(20)]
    # Expected names at 5, 11, 17; one false on Ferrari and missing the last name.
    assessment = runner.assess_events([3, 6, 12], cues)
    assert assessment["estimated_hits"] == 2
    assert assessment["estimated_misses"] == 1
    assert assessment["estimated_extra_events"] == 1
    assert not assessment["label_verified"]


def test_extra_events_in_positive_slot_and_silence_are_not_hidden():
    cues = [
        {"text": "ФирАя", "contains_wakeword": True, "actual_seconds": 2, "window_end_seconds": 7}
    ]
    assessment = runner.assess_events([1, 3, 5, 10], cues)
    assert assessment["estimated_hits"] == 1
    assert assessment["estimated_extra_events"] == 3
    assert assessment["outside_slot_events"] == [1, 10]


def test_full_fake_run_saves_14_wavs_scores_reports_and_frozen_model(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    summary = json.loads((output / "results.json").read_text())
    assert summary["complete"] and summary["completed_takes"] == 14
    assert len(list((output / "audio").glob("*.wav"))) == 14
    assert len(list((output / "takes").glob("*_scores.json"))) == 14
    assert len(summary["takes"]) == 14  # Score curves must not be parsed as take records.
    assert all(row["count_matches"] for row in summary["takes"])
    assert all(row["audio_protocol_valid"] for row in summary["takes"])
    assert all(row["requested_seconds"] == 20 for row in summary["takes"])
    assert all(row["capture"]["duration_seconds"] == 20 for row in summary["takes"])
    for row in summary["takes"]:
        audio = runner.read_wav(output / row["file"])
        assert len(audio) == 20 * runner.RATE
    assert all(not row["assessment"]["label_verified"] for row in summary["takes"])
    assert all(
        hashlib.sha256((output / row["file"]).read_bytes()).hexdigest() == row["wav_sha256"]
        for row in summary["takes"]
    )
    assert (output / "summary.csv").read_text().count("\n") == 15
    assert "<audio" in (output / "review.html").read_text()
    assert (output / "model/firaya_trial.onnx").read_bytes() == b"fake model"
    before = {path: path.read_bytes() for path in (output / "audio").glob("*.wav")}
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("must skip completed"))
    assert runner.main(args + ["--resume"]) == 0
    assert all(path.read_bytes() == value for path, value in before.items())


def test_resume_recovers_saved_wav_after_inference_failure_without_re_recording(
    monkeypatch, tmp_path
):
    args, output = setup_run(monkeypatch, tmp_path)
    infer = runner.infer_record
    monkeypatch.setattr(
        runner, "infer_record", lambda *args: (_ for _ in ()).throw(RuntimeError("ORT failed"))
    )
    assert runner.main(args) == 2
    wav = output / "audio/test_01_repeat_01.wav"
    original = wav.read_bytes()
    assert (
        json.loads((output / "takes/test_01_repeat_01.json").read_text())["status"] == "captured"
    )
    monkeypatch.setattr(runner, "infer_record", infer)
    calls = []
    monkeypatch.setattr(runner, "capture", lambda *a: (calls.append(a) or fake_capture(*a)))
    assert runner.main(args + ["--resume"]) == 0
    assert len(calls) == 13 and wav.read_bytes() == original


def test_partial_recording_is_preserved_and_re_recorded_as_new_take(monkeypatch, tmp_path):
    def partial(*args):
        pcm, metadata = fake_capture(*args)
        metadata["interrupted"] = True
        return pcm[:64000], metadata

    args, output = setup_run(monkeypatch, tmp_path, partial)
    assert runner.main(args) == 130
    partials = list((output / "audio").glob("*interrupted*.wav"))
    assert len(partials) == 1
    data = partials[0].read_bytes()
    monkeypatch.setattr(runner, "capture", fake_capture)
    assert runner.main(args + ["--resume"]) == 0
    assert partials[0].read_bytes() == data
    summary = json.loads((output / "results.json").read_text())
    assert summary["completed_takes"] == 14 and len(summary["takes"]) == 15


def test_corrupt_wav_on_resume_is_rejected_not_overwritten(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    wav = output / "audio/test_01_repeat_01.wav"
    wav.write_bytes(b"changed")
    monkeypatch.setattr(runner, "capture", lambda *args: pytest.fail("no record"))
    assert runner.main(args + ["--resume"]) == 2
    assert wav.read_bytes() == b"changed"


def test_owned_lock_is_removed_but_foreign_lock_is_preserved(monkeypatch, tmp_path):
    args, output = setup_run(monkeypatch, tmp_path)
    assert runner.main(args) == 0
    lock = output / ".recording.lock"
    lock.write_bytes(b"other process")
    assert runner.main(args + ["--resume"]) == 2
    assert lock.read_bytes() == b"other process"


def test_atomic_new_files_never_overwrite_existing_data(tmp_path):
    path = tmp_path / "one.wav"
    runner.write_atomic(path, b"first")
    with pytest.raises(FileExistsError):
        runner.write_atomic(path, b"second")
    assert path.read_bytes() == b"first"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("interrupt", [False, True])
def test_ros_capture_cue_clock_and_interrupted_audio_preservation(monkeypatch, interrupt, capsys):
    nodes = []

    class Probe:
        def __init__(self, topic):
            self.blocks = []
            self.sample_rate = 16000
            self.channels = 1
            self.gaps = 0
            self.calls = 0
            self.destroyed = False
            nodes.append(self)

        def destroy_node(self):
            self.destroyed = True

    def spin_once(node, timeout_sec):
        node.calls += 1
        if interrupt and node.calls == 8:
            raise KeyboardInterrupt
        node.blocks.append(np.zeros((8000, 1), dtype=np.int16))

    shutdown = []
    monkeypatch.setitem(
        sys.modules,
        "rclpy",
        SimpleNamespace(
            init=lambda args: None,
            ok=lambda: True,
            spin_once=spin_once,
            try_shutdown=lambda: shutdown.append(True),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "scripts.xvf3800_ros_channel_probe",
        SimpleNamespace(ChannelProbe=Probe, analyse_interleaved=lambda pcm: []),
    )
    pcm, metadata = runner.capture("/audio/mic", 20, runner.TESTS[0].cues(20))
    assert nodes[0].destroyed and shutdown == [True]
    assert metadata["interrupted"] == interrupt
    assert len(pcm) / 32000 == metadata["duration_seconds"]
    assert metadata["cues"][0]["actual_seconds"] == 2
    if interrupt:
        assert metadata["duration_seconds"] < 20
    else:
        assert metadata["duration_seconds"] == 20
        assert len(metadata["cues"]) == 5
        assert all(
            cue["stop_actual_seconds"] == cue["window_end_seconds"] - 1 for cue in metadata["cues"]
        )
        assert ">>> МОЛЧИ" in capsys.readouterr().out
