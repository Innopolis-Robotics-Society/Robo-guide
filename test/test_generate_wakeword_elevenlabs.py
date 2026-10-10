from __future__ import annotations

import json
import urllib.error
import wave

import pytest

from scripts import generate_wakeword_elevenlabs as tts


def plan():
    return tts.build_plan(["voice01", "voice02"], 1, "eleven_multilingual_v2")


def test_same_voices_settings_and_repeats_for_both_labels():
    data = plan()
    assert data["characters"] == sum(len(row["payload"]["text"]) for row in data["samples"])
    assert len({row["id"] for row in data["samples"]}) == len(data["samples"])
    groups = {}
    for row in data["samples"]:
        assert ("фирая" in row["payload"]["text"].lower()) == (row["kind"] == "positive")
        key = (
            row["voice_id"],
            json.dumps(row["payload"]["voice_settings"], sort_keys=True),
            row["payload"]["seed"],
        )
        groups.setdefault(row["kind"], set()).add(key)
    assert groups["positive"] == groups["negative"]
    assert data["review_required"] and not data["training_ready"]


@pytest.mark.parametrize(
    "voices,repeats", [([], 1), (["voice01", "voice01"], 1), (["../bad"], 1), (["voice01"], 0)]
)
def test_invalid_plan_rejected(voices, repeats):
    with pytest.raises(ValueError):
        tts.build_plan(voices, repeats, "model")


def test_default_is_offline_no_key_no_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tts, "api_request", lambda *args: pytest.fail("API must not be called"))
    monkeypatch.setattr(tts, "read_key", lambda: pytest.fail("key must not be read"))
    output = tmp_path / "pilot"
    assert tts.main(["--output", str(output)]) == 0
    assert not output.exists()
    assert "Предпросмотр" in capsys.readouterr().out


def test_execute_requires_limit_before_any_api_or_key(monkeypatch):
    monkeypatch.setattr(tts, "read_key", lambda: pytest.fail("key must not be read"))
    assert tts.main(["--execute", "--auto-voices"]) == 2


def test_execute_rejects_preview_voice_before_key(monkeypatch):
    monkeypatch.setattr(tts, "read_key", lambda: pytest.fail("key must not be read"))
    assert tts.main(["--execute", "--max-characters", "5000"]) == 2


def test_limit_rejected_without_output_or_request(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "api_request", lambda *args: pytest.fail("no paid API"))
    output = tmp_path / "pilot"
    with pytest.raises(ValueError, match="лимит"):
        tts.generate(plan(), output, "secret", 1, False)
    assert not output.exists()


def test_generation_resume_exact_hashes_no_duplicate_charge(monkeypatch, tmp_path):
    data = plan()
    calls = []

    def api(path, key, payload):
        assert key == "SECRET_NOT_FOR_DISK"
        assert "output_format=pcm_16000" in path
        calls.append(payload)
        return b"\x01\x00" * 1600, {"request-id": "request123"}

    monkeypatch.setattr(tts, "api_request", api)
    output = tmp_path / "pilot"
    assert tts.generate(data, output, "SECRET_NOT_FOR_DISK", 5000, False) == len(data["samples"])
    original_calls = len(calls)
    assert tts.generate(data, output, "SECRET_NOT_FOR_DISK", 5000, True) == 0
    assert len(calls) == original_calls
    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    assert len(rows) == original_calls
    assert all(row["review_required"] and not row["label_verified"] for row in rows)
    for row in rows:
        with wave.open(str(output / row["sample"]["file"]), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
    assert all(
        b"SECRET_NOT_FOR_DISK" not in path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    )


def test_timeout_keeps_attempt_and_never_repeats_on_resume(monkeypatch, tmp_path):
    data = plan()
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("timeout")

    monkeypatch.setattr(tts, "api_request", fail)
    output = tmp_path / "pilot"
    with pytest.raises(RuntimeError, match="timeout"):
        tts.generate(data, output, "secret", 5000, False)
    assert len(calls) == 1
    assert not (output / ".generation.lock").exists()
    assert len(list((output / "metadata").glob("*.attempt.json"))) == 1
    with pytest.raises(ValueError, match="незавершённый"):
        tts.generate(data, output, "secret", 5000, True)
    assert len(calls) == 1


def test_modified_plan_cannot_overwrite_existing_run(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "api_request", lambda *args: (b"\x01\x00" * 1600, {}))
    output = tmp_path / "pilot"
    tts.generate(plan(), output, "secret", 5000, False)
    monkeypatch.setattr(tts, "api_request", lambda *args: pytest.fail("no extra charge"))
    changed = tts.build_plan(["voice03"], 1, "eleven_multilingual_v2")
    with pytest.raises(ValueError, match="изменён план"):
        tts.generate(changed, output, "secret", 5000, True)
    with pytest.raises(ValueError, match="папка уже существует"):
        tts.generate(plan(), output, "secret", 5000, False)


def test_corrupt_wav_is_not_silently_regenerated(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "api_request", lambda *args: (b"\x01\x00" * 1600, {}))
    data = plan()
    output = tmp_path / "pilot"
    tts.generate(data, output, "secret", 5000, False)
    (output / data["samples"][0]["file"]).write_bytes(b"broken")
    monkeypatch.setattr(tts, "api_request", lambda *args: pytest.fail("no extra charge"))
    with pytest.raises(ValueError, match="повреждён"):
        tts.generate(data, output, "secret", 5000, True)


@pytest.mark.parametrize(
    "pcm,headers",
    [
        (b"", {}),
        (b"x" * 3201, {}),
        (b"\0" * 3200, {}),
        (b"RIFF" + b"x" * 3200, {}),
        (b"x" * 3200, {"Content-Type": "application/json"}),
    ],
)
def test_invalid_audio_is_not_wrapped_as_wav(pcm, headers):
    with pytest.raises(ValueError):
        tts.pcm_to_wav(pcm, headers)


def test_auto_selection_prefers_gender_diversity():
    voices = [
        {"voice_id": "voice01", "name": "A", "labels": {"gender": "male"}},
        {"voice_id": "voice02", "name": "B", "labels": {"gender": "male"}},
        {"voice_id": "voice03", "name": "C", "labels": {"gender": "female"}},
    ]
    assert tts.choose_voices(voices) == ["voice01", "voice03"]


def test_voice_listing_paginates_without_writing(monkeypatch):
    calls = []

    def api(path, key):
        calls.append(path)
        assert "language=ru" in path
        if len(calls) == 1:
            return json.dumps(
                {
                    "voices": [{"voice_id": "voice01", "name": "A"}],
                    "has_more": True,
                    "next_page_token": "next",
                }
            ).encode(), {}
        assert "next_page_token=next" in path
        return json.dumps(
            {"voices": [{"voice_id": "voice02", "name": "B"}], "has_more": False}
        ).encode(), {}

    monkeypatch.setattr(tts, "api_request", api)
    assert [row["voice_id"] for row in tts.available_voices("secret")] == ["voice01", "voice02"]
    assert len(calls) == 2


def test_request_uses_expected_endpoint_headers_and_does_not_retry(monkeypatch):
    calls = []

    class Response:
        headers = {"Content-Type": "audio/pcm"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == tts.MAX_AUDIO_BYTES + 1
            return b"\x01\x00" * 1600

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == (
                tts.API + "/v1/text-to-speech/voice01?output_format=pcm_16000"
            )
            assert request.get_header("Xi-api-key") == "secret"
            assert request.get_method() == "POST"
            assert json.loads(request.data) == {"text": "Фирая."}
            assert timeout == 90
            return Response()

    monkeypatch.setattr(tts.urllib.request, "build_opener", lambda *args: Opener())
    audio, headers = tts.api_request(
        "/v1/text-to-speech/voice01?output_format=pcm_16000", "secret", {"text": "Фирая."}
    )
    assert len(audio) == 3200 and headers["Content-Type"] == "audio/pcm"
    assert len(calls) == 1


def test_http_error_is_sanitized_and_never_retried(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 401, "DO_NOT_PRINT_SECRET", {}, None)

    monkeypatch.setattr(tts.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="HTTP 401") as caught:
        tts.api_request("/v1/text-to-speech/voice01", "secret", {"text": "test"})
    assert "DO_NOT_PRINT_SECRET" not in str(caught.value)
    assert len(calls) == 1


def test_active_lock_is_not_deleted_or_bypassed(monkeypatch, tmp_path):
    output = tmp_path / "pilot"
    output.mkdir()
    (output / ".generation.lock").write_bytes(b"other writer")
    monkeypatch.setattr(tts, "api_request", lambda *args: pytest.fail("no paid API"))
    with pytest.raises(FileExistsError):
        tts.generate(plan(), output, "secret", 5000, True)
    assert (output / ".generation.lock").read_bytes() == b"other writer"


def test_partial_run_can_resume_after_completed_request(monkeypatch, tmp_path):
    data = plan()
    output = tmp_path / "pilot"
    # Simulate Ctrl+C between requests (no in-flight request at interruption).
    original = tts.write_new

    def interrupt_after_result(path, content):
        original(path, content)
        if path.parent.name == "metadata" and not path.name.endswith(".attempt.json"):
            raise KeyboardInterrupt

    calls = []

    def api(*args):
        calls.append(args)
        return b"\x01\x00" * 1600, {}

    monkeypatch.setattr(tts, "api_request", api)
    monkeypatch.setattr(tts, "write_new", interrupt_after_result)
    with pytest.raises(KeyboardInterrupt):
        tts.generate(data, output, "secret", 5000, False)
    monkeypatch.setattr(tts, "write_new", original)
    assert tts.generate(data, output, "secret", 5000, True) == len(data["samples"]) - 1
    assert len(calls) == len(data["samples"])
