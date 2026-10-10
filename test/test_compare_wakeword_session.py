import numpy as np
import pytest

from scripts.compare_wakeword_session import (
    align_script_targets,
    assess_timing,
    timing_hints,
    words_with_times,
)
from scripts.train_wakeword_frames import supervised_indices


def test_token_times_reconstruct_words_not_kws_predictions():
    words = words_with_times(
        {"tokens": ["▁при", "вет", "▁фи", "рая"], "timestamps": [1, 1.1, 2, 2.2]}
    )
    assert [word["word"] for word in words] == ["привет", "фирая"]
    assert [word["start"] for word in words] == [1, 2]
    assert [word["end"] for word in words] == pytest.approx([1.18, 2.28])


def test_alias_ferrari_in_negative_slot_is_not_a_positive_name():
    record = {
        "requested_seconds": 8,
        "capture": {
            "cues": [
                {"actual_seconds": 2, "contains_wakeword": False, "text": "Феррари"},
                {"actual_seconds": 5, "contains_wakeword": True, "text": "ФирАя"},
            ]
        },
    }
    hints = timing_hints(record, {"tokens": ["▁феррари", "▁феррари"], "timestamps": [3, 6]})
    assert len(hints) == 1
    assert hints[0]["candidates"][0]["start"] == 6


def test_correct_counts_with_wrong_timing_do_not_pass():
    hint = {"candidates": [{"start": 2, "end": 2.5}], "ambiguous": False}
    result = assess_timing([5], [hint])
    assert result["hits"] == 0 and result["unmatched_events"] == [5]
    result = assess_timing([3, 3.5], [hint])
    assert result["hits"] == 1 and result["unmatched_events"] == [3.5]
    assert not result["label_verified"]


def test_multi_target_alignment_does_not_label_actual_ferrari_as_the_name():
    text = "ФирАя мы видели Феррари потом ФирАя"
    words = [
        {"word": word, "start": index + 5, "end": index + 5.4}
        for index, word in enumerate("ферая мы видели феррари потом феррая".split())
    ]
    result = align_script_targets(text, words)
    assert len(result) == 2
    assert [row["candidates"][0]["start"] for row in result] == [5, 10]
    record = {
        "requested_seconds": 30,
        "capture": {
            "cues": [
                {
                    "text": text,
                    "actual_seconds": 5,
                    "contains_wakeword": True,
                    "expected_events": 2,
                }
            ]
        },
    }
    hints = timing_hints(
        record,
        {
            "tokens": ["▁" + word["word"] for word in words],
            "timestamps": [word["start"] for word in words],
        },
    )
    assert len(hints) == 2 and not any(hint["ambiguous"] for hint in hints)
    assessment = assess_timing([5.5, 8.5], hints)
    assert assessment["hits"] == 1 and assessment["unmatched_events"] == [8.5]


def test_missing_name_does_not_steal_a_ferrari_distractor():
    text = "ФирАя мы видели Феррари потом ФирАя"
    words = [
        {"word": word, "start": index, "end": index + 0.4}
        for index, word in enumerate("мы видели феррари потом фирая".split())
    ]
    result = align_script_targets(text, words)
    assert result[0]["ambiguous"] and not result[0]["candidates"]
    assert result[1]["candidates"][0]["word"] == "фирая"


def test_frame_labels_do_not_mark_the_whole_positive_file_positive():
    positive, negative = supervised_indices(100, 2, 2.5)
    times = (np.arange(100) + 1) * 0.08
    assert np.all((times[positive] >= 2.9) & (times[positive] <= 3.5))
    assert np.all((times[negative] < 2) | (times[negative] > 4.5))
    assert not set(positive) & set(negative)
