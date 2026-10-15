import json
import wave

import numpy as np

from scripts.collect_wakeword_candidates import AudioRing, CandidateTracker, CandidateWriter


def candidate(tracker, end=48000, at=0.0):
    return tracker.add(
        session="cap",
        end_sample=end,
        score=0.93,
        model_sha256="sha",
        received_at=at,
        tts_active=False,
    )


def test_ring_returns_exact_window_only_after_audio_arrives():
    ring = AudioRing(max_seconds=10)
    for first in range(0, 64000, 256):
        ring.push("cap", first, np.arange(first, first + 256, dtype=np.int64).astype(np.int16))
    assert ring.extract("cap", 48000 - 100, 48000 + 16000 + 1) is None
    window = ring.extract("cap", 1000, 1300)
    np.testing.assert_array_equal(window, np.arange(1000, 1300).astype(np.int16))
    assert ring.extract("other", 1000, 1300) is None


def test_veto_by_asr_becomes_negative_and_unadmitted_stays_unlabeled():
    tracker = CandidateTracker(admit_timeout=1.5, outcome_timeout=25)
    vetoed = candidate(tracker, end=48000)
    ignored = candidate(tracker, end=96000)
    confirmed = candidate(tracker, end=144000)
    tracker.on_admit("cap", 48000, 7)
    tracker.on_admit("cap", 144000, 8)
    tracker.on_text(7, "так")
    tracker.on_result(7, "wake_not_confirmed")
    tracker.on_result(8, "wakeword_only")
    tracker.expire(2.0)
    assert (vetoed.outcome, ignored.outcome, confirmed.outcome) == (
        "negative",
        "not_admitted",
        "confirmed",
    )
    assert {c.number for c in tracker.ready()} == {1, 2, 3}
    assert vetoed.asr_text == "так"


def test_admitted_without_result_times_out_as_unresolved():
    tracker = CandidateTracker(admit_timeout=1.5, outcome_timeout=25)
    late = candidate(tracker)
    tracker.on_admit("cap", 48000, 3)
    tracker.expire(10.0)
    assert not late.outcome
    tracker.expire(30.0)
    assert late.outcome == "unresolved"


def test_negative_is_written_in_raw_dataset_format(tmp_path):
    tracker = CandidateTracker(admit_timeout=1.5, outcome_timeout=25)
    c = candidate(tracker)
    tracker.on_admit("cap", 48000, 7)
    tracker.on_result(7, "wake_not_confirmed")
    writer = CandidateWriter(tmp_path, "kws20261015T180000Z")
    path = writer.save(c, np.zeros(64000, dtype=np.int16), 3.0, 1.0)
    session_dir = tmp_path / "raw/negative/train/kws20261015T180000Z"
    assert path == session_dir / "kws_false_trigger_001.wav"
    with wave.open(str(path)) as stream:
        shape = (stream.getnchannels(), stream.getframerate(), stream.getnframes())
    assert shape == (1, 16000, 64000)
    row = json.loads((path.parent / "manifest.jsonl").read_text())
    assert row["kind"] == "negative" and row["status"] == "accepted"
    assert row["file"] == "negative/train/kws20261015T180000Z/kws_false_trigger_001.wav"
    assert row["kws_score"] == 0.93 and row["asr_status"] == "wake_not_confirmed"
    review_manifest = tmp_path / "robot_candidates/kws20261015T180000Z/manifest.jsonl"
    review = json.loads(review_manifest.read_text())
    assert review["outcome"] == "negative"


def test_unlabeled_candidates_never_enter_raw_negatives(tmp_path):
    tracker = CandidateTracker(admit_timeout=1.5, outcome_timeout=25)
    c = candidate(tracker)
    tracker.expire(5.0)
    writer = CandidateWriter(tmp_path, "s")
    path = writer.save(c, np.zeros(16000, dtype=np.int16), 3.0, 1.0)
    assert path == tmp_path / "robot_candidates/s/not_admitted" / "cand_0001.wav"
    assert not (tmp_path / "raw").exists()
