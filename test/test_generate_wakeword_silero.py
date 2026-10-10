from __future__ import annotations

import hashlib
import io
import json
import sys
import wave
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import generate_wakeword_silero as silero


def test_preview_has_no_load_download_or_files(monkeypatch, tmp_path):
    monkeypatch.setattr(silero, "load_model", lambda *args: pytest.fail("no model load"))
    monkeypatch.setattr(silero, "download_model", lambda *args: pytest.fail("no download"))
    output = tmp_path / "preview"
    assert silero.main(["--output", str(output), "--download-model"]) == 0
    assert not output.exists()


def test_pronunciation_separates_three_stresses_from_training():
    plan = silero.make_plan("pronunciation", list(silero.SPEAKERS), None)
    assert len(plan["samples"]) == 44
    assert plan["split"] == "review_only" and not plan["training_ready"]
    assert {row["stress"] for row in plan["samples"] if row["kind"] == "positive"} == {
        "a",
        "i",
        "ya",
    }


@pytest.mark.parametrize("stress,word", [("a", "Фир+ая"), ("i", "Ф+ирая"), ("ya", "Фира+я")])
def test_pilot_same_speakers_rates_for_both_labels(stress, word):
    plan = silero.make_plan("pilot", list(silero.SPEAKERS), stress)
    assert len(plan["samples"]) == 264
    assert len({row["id"] for row in plan["samples"]}) == 264
    assert len({row["file"] for row in plan["samples"]}) == 264
    groups = {}
    for row in plan["samples"]:
        params = row["parameters"]
        assert ("Фирая" in row["text"]) == (row["kind"] == "positive")
        if row["kind"] == "positive":
            assert word in params["text_with_stress"]
        groups.setdefault(row["kind"], set()).add((params["speaker"], params["rate_percent"]))
    assert groups["positive"] == groups["negative"]
    assert plan["split"] == "train_only" and plan["review_required"]


def test_hard_negatives_have_explicit_family_split_and_no_target_word():
    plan = silero.make_plan("hard_negatives", list(silero.SPEAKERS), "a")
    assert len(plan["samples"]) == 1560
    assert len({row["id"] for row in plan["samples"]}) == 1560
    assert len({row["file"] for row in plan["samples"]}) == 1560
    for row in plan["samples"]:
        if row["kind"] == "negative":
            assert "фирая" not in row["text"].lower()
        else:
            assert row["stress"] == "a" and row["training_split"] == "train"
    for index, (_, held_out) in enumerate(silero.HARD_WORDS, 1):
        family = [row for row in plan["samples"] if f"/hard_{index:02d}_" in row["file"]]
        assert len(family) == 24  # 4 voices * 3 rates * word/phrase.
        assert {row["training_split"] for row in family} == {"val" if held_out else "train"}


@pytest.mark.parametrize(
    "mode,speakers,stress",
    [
        ("pilot", ["ru_a"], None),
        ("pilot", ["ru_a"], "bad"),
        ("pilot", ["../x"], "a"),
        ("pilot", ["ru_a", "ru_a"], "a"),
        ("bad", ["ru_a"], "a"),
    ],
)
def test_invalid_plan(mode, speakers, stress):
    with pytest.raises(ValueError):
        silero.make_plan(mode, speakers, stress)


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.full(1600, 100, dtype="<i2").tobytes())
    return buffer.getvalue()


def test_generate_resume_integrity_and_never_overwrite(monkeypatch, tmp_path):
    plan = silero.make_plan("pronunciation", ["ru_a"], None)
    model = SimpleNamespace(speakers=["ru_a"])
    calls = []

    def fake(model, params):
        calls.append(params)
        return wav_bytes()

    monkeypatch.setattr(silero, "synthesize", fake)
    output = tmp_path / "pilot"
    assert silero.generate(model, "sha", plan, output, False) == 11
    manifest = (output / "manifest.jsonl").read_bytes()
    assert silero.generate(model, "sha", plan, output, True) == 0
    assert len(calls) == 11
    assert (output / "manifest.jsonl").read_bytes() == manifest
    rows = [json.loads(line) for line in manifest.splitlines()]
    assert all(not row["label_verified"] and row["review_required"] for row in rows)
    assert all(row["sha256"] == hashlib.sha256(wav_bytes()).hexdigest() for row in rows)
    assert "Фир+ая" in (output / "review.html").read_text()
    with pytest.raises(ValueError, match="папка уже существует"):
        silero.generate(model, "sha", plan, output, False)
    with pytest.raises(ValueError, match="план или вес изменён"):
        silero.generate(model, "new-sha", plan, output, True)


def test_unknown_speaker_rejected_before_creating_output(tmp_path):
    plan = silero.make_plan("pronunciation", ["ru_a"], None)
    with pytest.raises(ValueError, match="неизвестные голоса"):
        silero.generate(SimpleNamespace(speakers=["ru_b"]), "sha", plan, tmp_path / "new", False)
    assert not (tmp_path / "new").exists()


def test_atomic_write_never_overwrites_or_leaves_temp(tmp_path):
    target = tmp_path / "a.wav"
    silero.write_new(target, b"original")
    with pytest.raises(FileExistsError):
        silero.write_new(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.wav"]


def test_corrupt_wav_refused_on_resume(monkeypatch, tmp_path):
    plan = silero.make_plan("pronunciation", ["ru_a"], None)
    model = SimpleNamespace(speakers=["ru_a"])
    monkeypatch.setattr(silero, "synthesize", lambda *args: wav_bytes())
    output = tmp_path / "pilot"
    silero.generate(model, "sha", plan, output, False)
    (output / plan["samples"][0]["file"]).write_bytes(b"bad")
    monkeypatch.setattr(silero, "synthesize", lambda *args: pytest.fail("no regeneration"))
    with pytest.raises(ValueError, match="повреждён"):
        silero.generate(model, "sha", plan, output, True)


def test_html_escapes_model_text_and_paths():
    plan = silero.make_plan("pronunciation", ["ru_a"], None)
    plan["samples"][0]["parameters"]["text_with_stress"] = "<script>alert(1)</script>"
    page = silero.review_html(plan["samples"]).decode()
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_failed_download_removes_only_own_temp(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise OSError("network timeout")

    monkeypatch.setattr(silero.urllib.request, "urlopen", fail)
    target = tmp_path / "model.pt"
    with pytest.raises(OSError, match="timeout"):
        silero.download_model(target)
    assert not list(tmp_path.iterdir())


def test_cached_model_is_never_downloaded_again(monkeypatch, tmp_path):
    target = tmp_path / "model.pt"
    target.write_bytes(b"existing")
    monkeypatch.setattr(silero.urllib.request, "urlopen", lambda *args: pytest.fail("no network"))
    silero.download_model(target)
    assert target.read_bytes() == b"existing"


def fake_torch(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            manual_seed=lambda seed: None,
            inference_mode=nullcontext,
        ),
    )


class AudioTensor:
    def __init__(self, audio):
        self.audio = audio

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.audio


def test_ssml_and_resample_produce_true_mono_pcm16_at_16k(monkeypatch):
    fake_torch(monkeypatch)
    calls = []

    def apply_tts(**kwargs):
        calls.append(kwargs)
        audio = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)
        return AudioTensor(audio)

    params = silero.make_plan("pilot", ["ru_a"], "a")["samples"][0]["parameters"]
    data = silero.synthesize(SimpleNamespace(apply_tts=apply_tts), params)
    assert calls[0]["sample_rate"] == 24000
    assert calls[0]["speaker"] == "ru_a"
    assert 'rate="85%"' in calls[0]["ssml_text"] and "Фир+ая" in calls[0]["ssml_text"]
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.getnframes() == 16000
        pcm = np.frombuffer(wav.readframes(16000), dtype="<i2")
    assert 9000 < int(np.max(pcm)) < 11000  # Original amplitude, not peak-normalized.


@pytest.mark.parametrize("audio", [np.zeros(2400), np.full(2400, np.nan), np.ones(12)])
def test_bad_tts_output_rejected(monkeypatch, audio):
    fake_torch(monkeypatch)
    model = SimpleNamespace(apply_tts=lambda **kwargs: AudioTensor(audio))
    params = silero.make_plan("pronunciation", ["ru_a"], None)["samples"][0]["parameters"]
    with pytest.raises(ValueError):
        silero.synthesize(model, params)


def test_atomic_write_preserves_active_lock(monkeypatch, tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    (output / ".generation.lock").write_bytes(b"other writer")
    model = SimpleNamespace(speakers=["ru_a"])
    plan = silero.make_plan("pronunciation", ["ru_a"], None)
    monkeypatch.setattr(silero, "synthesize", lambda *args: pytest.fail("no synthesis"))
    with pytest.raises(FileExistsError):
        silero.generate(model, "sha", plan, output, True)
    assert (output / ".generation.lock").read_bytes() == b"other writer"


def test_main_reports_missing_dependency_and_failed_download(monkeypatch, capsys):
    def fail(*args):
        raise silero.urllib.error.URLError("SSL timeout")

    monkeypatch.setattr(silero, "download_model", fail)
    assert silero.main(["--execute", "--download-model"]) == 2
    assert "SSL timeout" in capsys.readouterr().err

    def missing(*args):
        raise ImportError("No module named torch")

    monkeypatch.setattr(silero, "load_model", missing)
    assert silero.main(["--execute"]) == 2
    assert "torch" in capsys.readouterr().err
