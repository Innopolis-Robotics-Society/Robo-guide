from __future__ import annotations

import hashlib
import wave

import numpy as np
import pytest

from scripts import prepare_wakeword_dataset as prep


def source_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(samples.astype("<i2").tobytes())


def test_keyword_hints_use_original_token_timestamps():
    text = "вот фирая потом"
    hints = prep.keyword_hints(list(text), [i * 0.04 for i in range(len(text))])
    assert len(hints) == 1
    assert hints[0]["word"] == "фирая"
    assert hints[0]["exact"]
    assert hints[0]["start"] == pytest.approx(0.16)
    assert hints[0]["end"] == pytest.approx(0.4)


def test_misrecognised_name_is_only_a_hint():
    text = "феррари"
    hints = prep.keyword_hints(list(text), [i * 0.04 for i in range(len(text))])
    assert len(hints) == 1
    assert not hints[0]["exact"]
    assert prep.keyword_hints(["фирая"], []) == []
    assert prep.keyword_hints(list("привет"), [0.0] * 6) == []


def test_vad_merges_short_pauses_inside_word():
    probabilities = [0.0] * 10 + [0.9] * 8 + [0.0] * 3 + [0.9] * 8 + [0.0] * 12
    regions = prep.speech_regions(probabilities, len(probabilities) * 0.032)
    assert len(regions) == 1
    assert regions[0][0] == pytest.approx(0.32)
    assert regions[0][1] == pytest.approx(29 * 0.032)


def test_vad_bounds_do_not_extend_past_source():
    regions = prep.speech_regions([0.9] * 80 + [0.0] * 20, 2.0)
    assert regions == [(0.0, 2.0)]


def test_quiet_single_speech_region_keeps_padding():
    start, end, method, reasons = prep.choose_crop(
        [(0.8, 1.4)],
        [{"start": 0.9, "end": 1.3, "distance": 0}],
        4.4,
        False,
        0.2,
    )
    assert start == pytest.approx(0.6)
    assert end == pytest.approx(1.6)
    assert method == "single_vad_region"
    assert not reasons


def test_crowd_hint_always_requires_review():
    start, end, method, reasons = prep.choose_crop(
        [(0.0, 4.4)],
        [{"start": 1.0, "end": 1.6, "distance": 0}],
        4.4,
        True,
        0.2,
    )
    assert start == pytest.approx(0.65)
    assert end == pytest.approx(1.95)
    assert method == "asr_hint"
    assert reasons


@pytest.mark.parametrize(
    "region,hint,duration",
    [
        ((2.56, 3.168), {"start": 1.92, "end": 2.80, "distance": 1}, 4.384),
        ((4.16, 4.384), {"start": 3.88, "end": 4.20, "distance": 2}, 4.384),
    ],
)
def test_quiet_vad_does_not_cut_off_disagreeing_asr_hint(region, hint, duration):
    start, end, method, reasons = prep.choose_crop([region], [hint], duration, False, 0.2)
    assert start <= hint["start"]
    assert end >= hint["end"]
    assert start <= region[0] and end >= region[1]
    assert method == "vad_asr_union"
    assert reasons


def test_ambiguous_recording_is_not_cut_arbitrarily():
    start, end, method, reasons = prep.choose_crop(
        [(0.0, 4.4)],
        [],
        4.4,
        True,
        0.2,
    )
    assert (start, end, method) == (0.0, 4.4, "full_recording")
    assert reasons


def test_crop_is_exact_slice_of_pcm_and_never_overwrites(tmp_path):
    samples = np.arange(32000, dtype=np.int16)
    output = tmp_path / "crop.wav"
    first, last = prep.write_crop(output, samples, 0.2, 0.8)
    actual, rate = prep.read_wav(output)
    assert rate == 16000
    assert np.array_equal(actual, samples[first:last])
    with pytest.raises(FileExistsError):
        prep.write_crop(output, samples, 0.2, 0.8)


def test_positive_crop_preserves_background_and_original(tmp_path):
    raw = tmp_path / "raw"
    output = tmp_path / "out"
    output.mkdir()
    path = raw / "positive/train/s01/d01/crowd_2m_front_01.wav"
    samples = np.random.default_rng(1).integers(-1000, 1000, 64000, dtype=np.int16)
    source_wav(path, samples)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    class Analyser:
        def speech(self, _):
            return [(0.0, 4.0)]

        def decode(self, _):
            return {
                "text": "фирая фон",
                "hints": [
                    {"start": 1.0, "end": 1.6, "distance": 0, "word": "фирая"},
                ],
            }

    row = prep.process_positive(path, raw, output, Analyser(), 0.2)
    actual, _ = prep.read_wav(output / row["candidate"])
    assert np.array_equal(actual, samples[row["crop_start_sample"] : row["crop_end_sample"]])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert row["review_required"] and not row["label_verified"]


def test_negative_windows_are_all_pending_even_without_asr_hints(tmp_path):
    raw = tmp_path / "raw"
    output = tmp_path / "out"
    output.mkdir()
    path = raw / "negative/train/d01/crowd_01.wav"
    samples = np.arange(64000, dtype=np.int16)
    source_wav(path, samples)

    class Analyser:
        def decode(self, _):
            return {"text": "обычный разговор", "hints": []}

    rows, scan = prep.process_negative(path, raw, output, Analyser(), 2.0)
    assert len(rows) == 2
    assert all(row["review_required"] and not row["label_verified"] for row in rows)
    assert all(row["status"] == "negative_unverified" for row in rows)
    joined = np.concatenate([prep.read_wav(output / row["candidate"])[0] for row in rows])
    assert np.array_equal(joined, samples)
    summary = prep.write_reports(output, raw, rows, [scan])
    assert not summary["training_ready"]
    assert (output / "review.csv").exists()
    assert (output / "review.html").exists()


def test_possible_keyword_flags_nearby_negative_windows(tmp_path):
    raw = tmp_path / "raw"
    output = tmp_path / "out"
    output.mkdir()
    path = raw / "negative/train/d01/crowd_01.wav"
    source_wav(path, np.ones(64000, dtype=np.int16))

    class Analyser:
        def decode(self, _):
            return {
                "text": "фирая",
                "hints": [
                    {"word": "фирая", "start": 1.9, "end": 2.2, "distance": 0},
                ],
            }

    rows, _ = prep.process_negative(path, raw, output, Analyser(), 2.0)
    assert all(row["status"] == "possible_keyword" for row in rows)
