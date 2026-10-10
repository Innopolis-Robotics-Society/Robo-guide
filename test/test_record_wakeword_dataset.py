from __future__ import annotations

import argparse
import json
import sys
import types
import wave
from collections import Counter

import pytest

from scripts import record_wakeword_dataset as recorder


def arguments(root, **overrides):
    values = dict(
        kind="positive",
        root=root,
        speaker="s01",
        split=None,
        session=None,
        resume=False,
        seconds=4.0,
        topic="/audio/mic",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_positive_plan_has_20_15_15_unique_files(tmp_path):
    plan = recorder.build_plan(arguments(tmp_path))
    assert len(plan) == 50
    assert Counter(take.condition for take in plan) == {
        "quiet_1m_front": 20,
        "quiet_3m_front": 15,
        "crowd_2m_front": 15,
    }
    assert len({take.filename for take in plan}) == 50


@pytest.mark.parametrize(
    "speaker,split",
    [
        ("s01", "train"),
        ("s08", "train"),
        ("s09", "val"),
        ("s10", "val"),
        ("s11", "test"),
        ("s12", "test"),
    ],
)
def test_speaker_split(speaker, split):
    assert recorder.speaker_split(speaker) == split


def test_more_speakers_require_explicit_split():
    with pytest.raises(ValueError, match="--split"):
        recorder.speaker_split("s13")


def test_session_auto_increment_and_dry_run_do_not_create_directories(tmp_path):
    root = tmp_path / "raw"
    args = arguments(root)
    assert recorder.session_path(args) == root / "positive/train/s01/d01"
    assert not root.exists()
    parent = root / "positive/train/s01"
    (parent / "d01").mkdir(parents=True)
    (parent / "d03").mkdir()
    assert recorder.next_session(parent) == "d04"
    (parent / "d99").mkdir()
    assert recorder.next_session(parent) == "d100"


def test_existing_session_requires_resume(tmp_path):
    (tmp_path / "positive/train/s01/d01").mkdir(parents=True)
    with pytest.raises(ValueError, match="уже существует"):
        recorder.session_path(arguments(tmp_path, session="d01"))
    assert recorder.session_path(arguments(tmp_path, session="d01", resume=True)).is_dir()


def test_resume_requires_an_existing_explicit_session(tmp_path):
    with pytest.raises(ValueError, match="--session"):
        recorder.session_path(arguments(tmp_path, resume=True))
    with pytest.raises(ValueError, match="не найдена"):
        recorder.session_path(arguments(tmp_path, session="d01", resume=True))


def test_cannot_put_same_speaker_in_two_splits(tmp_path):
    (tmp_path / "positive/train/s01/d01").mkdir(parents=True)
    with pytest.raises(ValueError, match="нельзя менять выборку"):
        recorder.session_path(arguments(tmp_path, split="val"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"speaker": "../s01"},
        {"speaker": "s00"},
        {"session": "../../d01"},
        {"seconds": 0},
        {"seconds": float("nan")},
        {"seconds": float("inf")},
    ],
)
def test_invalid_arguments_are_rejected(tmp_path, overrides):
    with pytest.raises(ValueError):
        recorder.session_path(arguments(tmp_path, **overrides))


def test_negative_plan_and_session(tmp_path):
    args = arguments(
        tmp_path,
        kind="negative",
        split="train",
        condition="crowd",
        takes=6,
        seconds=300,
    )
    assert recorder.session_path(args) == tmp_path / "negative/train/d01"
    assert len(recorder.build_plan(args)) == 6
    assert recorder.build_plan(args)[-1].filename == "crowd_06.wav"


def test_wav_writer_preserves_format_and_refuses_overwrite(tmp_path):
    output = tmp_path / "take.wav"
    pcm = b"\x01\x00" * 16000
    recorder.write_wav(output, pcm)
    original = output.read_bytes()
    with wave.open(str(output), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 16000
        assert recording.readframes(16000) == pcm
    with pytest.raises(FileExistsError):
        recorder.write_wav(output, b"\x02\x00")
    assert output.read_bytes() == original


def test_cli_dry_run_requires_no_ros_and_writes_nothing(tmp_path, capsys):
    root = tmp_path / "raw"
    assert (
        recorder.main(
            [
                "positive",
                "--speaker",
                "s09",
                "--root",
                str(root),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "выборка: val" in capsys.readouterr().out
    assert not root.exists()


def test_one_enter_per_take_automatically_saves_and_records_metadata(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    plan = [recorder.Take("quiet_1m_front", "1 метр", number) for number in (1, 2)]
    prompts = []

    def start(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", start)
    monkeypatch.setattr(recorder, "_capture", lambda *_: (b"\x01\x00" * 64000, {"gaps": 0}))
    recorder.record_plan(args, directory, plan)
    assert len(prompts) == 2
    assert all("для записи" in prompt for prompt in prompts)
    assert len(list(directory.glob("*.wav"))) == 2
    assert not (tmp_path / "rejected").exists()
    entries = [
        json.loads(line) for line in (directory / "manifest.jsonl").read_text().splitlines()
    ]
    assert [entry["status"] for entry in entries] == ["accepted", "accepted"]
    assert all((tmp_path / entry["file"]).exists() for entry in entries)


def test_no_audio_does_not_advance_take(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    answers = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    captures = []

    def capture(*_):
        captures.append(1)
        if len(captures) == 1:
            raise recorder.CaptureError("нет данных")
        return b"\x01\x00" * 64000, {"gaps": 0}

    monkeypatch.setattr(recorder, "_capture", capture)
    recorder.record_plan(args, directory, [recorder.Take("quiet_1m_front", "1 метр", 1)])
    assert len(captures) == 2
    assert len(list(directory.glob("*.wav"))) == 1


def test_resume_skips_already_recorded_wav(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    take = recorder.Take("quiet_1m_front", "1 метр", 1)
    output = directory / take.filename
    recorder.write_wav(output, b"\x01\x00" * 64000)
    recorder._log_take(directory, args, take, output, {}, "accepted")
    original = output.read_bytes()
    monkeypatch.setattr(recorder, "_capture", lambda *_: pytest.fail("не нужно записывать снова"))
    recorder.record_plan(args, directory, [take])
    assert output.read_bytes() == original


def test_resume_archives_unconfirmed_wav_and_records_it_again(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    take = recorder.Take("quiet_1m_front", "1 метр", 1)
    recorder.write_wav(directory / take.filename, b"\x01\x00" * 64000)
    recorder._log_take(directory, args, take, directory / take.filename, {}, "recorded")
    prompts = []

    def accept(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", accept)
    monkeypatch.setattr(recorder, "_capture", lambda *_: (b"\x02\x00" * 64000, {"gaps": 0}))
    recorder.record_plan(args, directory, [take])
    assert len(prompts) == 1
    assert "для записи" in prompts[0]
    assert len(list((tmp_path / "rejected").rglob("*.wav"))) == 1
    with wave.open(str(directory / take.filename), "rb") as recording:
        assert recording.readframes(1) == b"\x02\x00"
    entry = recorder._last_entry(directory, tmp_path, directory / take.filename)
    assert entry["status"] == "accepted"


def test_enter_only_prompt_rejects_letters(monkeypatch):
    answers = iter(["r", "q", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert recorder._wait_for_enter("Enter: ") is None


def test_ctrl_c_keeps_completed_takes(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    calls = []

    def start(_):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr("builtins.input", start)
    monkeypatch.setattr(recorder, "_capture", lambda *_: (b"\x01\x00" * 64000, {"gaps": 0}))
    plan = [recorder.Take("quiet_1m_front", "1 метр", number) for number in (1, 2)]
    with pytest.raises(KeyboardInterrupt):
        recorder.record_plan(args, directory, plan)
    assert (directory / plan[0].filename).exists()
    assert not (directory / plan[1].filename).exists()
    entry = recorder._last_entry(directory, tmp_path, directory / plan[0].filename)
    assert entry["status"] == "accepted"


@pytest.fixture
def fake_audio(monkeypatch):
    import numpy as np

    state = dict(clock=0.0, frames=0, gaps=0, value=100, rate=16000, channels=1, silent=False)

    class Probe:
        def __init__(self, _):
            self.blocks = []
            self.gaps = 0
            self.sample_rate = 0
            self.channels = 0

        def _on_audio(self, message):
            self.sample_rate = message.sample_rate
            self.channels = message.channels
            self.blocks.append(
                np.asarray(message.data, dtype=np.int16).reshape(-1, message.channels)
            )
            self.gaps = state["gaps"]

        def destroy_node(self):
            state["destroyed"] = True

    def spin_once(node, timeout_sec):
        state["clock"] += timeout_sec
        if not state["silent"] and state["clock"] <= state.get("stop_after", float("inf")):
            message = types.SimpleNamespace(
                sample_rate=state["rate"],
                channels=state["channels"],
                data=[state["value"]] * int(state["rate"] * timeout_sec) * state["channels"],
            )
            node._on_audio(message)

    ros = types.ModuleType("rclpy")
    ros.init = lambda **_: None
    ros.ok = lambda: True
    ros.spin_once = spin_once
    ros.try_shutdown = lambda: state.update(shutdown=True)
    probe = types.ModuleType("scripts.xvf3800_ros_channel_probe")
    probe.ChannelProbe = Probe
    probe.analyse_interleaved = lambda _: [
        {
            "channel": 0,
            "rms_dbfs": -30.0,
            "peak_dbfs": -20.0,
            "clipped_samples": int(state["value"] == 32767),
        }
    ]
    monkeypatch.setitem(sys.modules, "rclpy", ros)
    monkeypatch.setitem(sys.modules, "scripts.xvf3800_ros_channel_probe", probe)
    monkeypatch.setattr(recorder.time, "monotonic", lambda: state["clock"])
    return state


def test_capture_waits_for_audio_and_keeps_preroll(fake_audio, capsys):
    pcm, metrics = recorder._capture("/audio/mic", 4, "ГОВОРИ: Фирая")
    assert 4.3 <= metrics["duration_seconds"] < 4.6
    assert len(pcm) == int(metrics["duration_seconds"] * 16000) * 2
    assert "ГОВОРИ: Фирая" in capsys.readouterr().out
    assert fake_audio["destroyed"] and fake_audio["shutdown"]


@pytest.mark.parametrize(
    "changes",
    [
        {"gaps": 1},
        {"value": 32767},
        {"value": 0},
        {"rate": 48000},
        {"channels": 2},
        {"stop_after": 0.5},
    ],
)
def test_capture_does_not_reject_audio_for_quality(fake_audio, changes, tmp_path):
    fake_audio.update(changes)
    pcm, metrics = recorder._capture("/audio/mic", 4, "ГОВОРИ")
    assert pcm
    assert metrics["sample_rate"] == fake_audio["rate"]
    assert metrics["channels"] == fake_audio["channels"]
    output = tmp_path / "take.wav"
    recorder.write_wav(output, pcm, metrics["sample_rate"], metrics["channels"])
    with wave.open(str(output), "rb") as recording:
        assert recording.getframerate() == fake_audio["rate"]
        assert recording.getnchannels() == fake_audio["channels"]
        assert recording.readframes(recording.getnframes()) == pcm
    assert fake_audio["destroyed"] and fake_audio["shutdown"]


def test_capture_still_handles_missing_topic(fake_audio):
    fake_audio["silent"] = True
    with pytest.raises(recorder.CaptureError):
        recorder._capture("/audio/mic", 4, "ГОВОРИ")
    assert fake_audio["destroyed"] and fake_audio["shutdown"]


def test_resume_does_not_check_saved_audio_length(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    directory = recorder.session_path(args)
    directory.mkdir(parents=True)
    take = recorder.Take("quiet_1m_front", "1 метр", 1)
    output = directory / take.filename
    recorder.write_wav(output, b"\x00\x00")
    recorder._log_take(directory, args, take, output, {}, "accepted")
    monkeypatch.setattr(recorder, "_capture", lambda *_: pytest.fail("дубль уже сохранён"))
    recorder.record_plan(args, directory, [take])
    assert output.exists()
