import numpy as np
import pytest

from scripts import wakeword_phonetic as phonetic


def test_character_targets_preserve_the_entire_name_and_normalize_russian():
    labels = phonetic.encode_text("ФирАя, расскажи ещё!")
    assert "".join(phonetic.ALPHABET[index] for index in labels) == "фирая расскажи еще"
    assert phonetic.encode_text("Фирая").tolist() != phonetic.encode_text("Фира").tolist()
    assert 0 not in labels
    with pytest.raises(ValueError):
        phonetic.encode_text("123")


def test_ctc_blank_separates_repeated_letters():
    indices = [0, 1, 1, 0, 0, 1, 0]
    probability = np.eye(len(phonetic.ALPHABET))[indices]
    assert phonetic.decode_characters(probability) == ""  # Both spaces trim away.
    char = phonetic.ALPHABET.index("р")
    probability = np.eye(len(phonetic.ALPHABET))[[char, char, 0, char, 0]]
    assert phonetic.decode_characters(probability) == "рр"


def test_timing_uses_vad_and_never_full_recording_end_as_word_end():
    row = {
        "method": "single_vad_region",
        "vad_regions": [[1.0, 1.7]],
        "crop_start_seconds": 0.8,
        "crop_end_seconds": 1.9,
        "source_seconds": 4,
    }
    assert phonetic.reviewed_interval(row) == (1.0, 1.7)
    row["method"] = "vad_asr_union"
    assert phonetic.reviewed_interval(row) == pytest.approx((1.0, 1.7))
    row["method"] = "asr_hint"
    assert phonetic.reviewed_interval(row) == pytest.approx((1.15, 1.55))
    row["method"] = "full_recording"
    assert phonetic.reviewed_interval(row) is None


def test_negative_segments_cannot_form_false_confirmation_across_a_gap():
    groups = phonetic.consecutive_groups([8, 16, 40, 48, 64])
    assert [group.tolist() for group in groups] == [[8, 16], [40, 48], [64]]
    assert phonetic.consecutive_groups([]) == []


def test_complete_word_supervision_waits_for_the_ending_and_rejects_early_prefix():
    positive, negative = phonetic.word_positions(600, (1.0, 1.8), complete_word=True)
    assert (positive / 100 - 2 >= 2.0).all()
    assert (positive / 100 - 2 <= 2.44).all()
    assert 328 in negative  # 1.28s: only the early prefix has arrived.
    assert 368 not in negative  # 1.68s: uncertain last part, not mislabeled.
    assert not set(positive) & set(negative)


def test_phonetic_head_variable_time_ctc_and_kws_gradients():
    torch = pytest.importorskip("torch")
    torch.set_num_threads(2)
    model = phonetic.build_model(np.zeros(80, np.float32), np.ones(80, np.float32))
    value = torch.randn(2, 160, 80, requires_grad=True)
    logits, probabilities = model(value), model.ctc(value)
    assert logits.shape == (2, 1)
    assert probabilities.shape == (2, 40, len(phonetic.ALPHABET))
    labels = torch.from_numpy(phonetic.encode_text("Фирая"))
    loss = (
        torch.nn.CTCLoss()(
            probabilities.transpose(0, 1),
            labels.repeat(2),
            torch.tensor([40, 40]),
            torch.tensor([5, 5]),
        )
        + logits.sum()
    )
    loss.backward()
    assert torch.isfinite(value.grad).all()
    assert model.ctc(torch.randn(1, 201, 80)).shape[1] == 51
    finer = phonetic.build_model(np.zeros(80, np.float32), np.ones(80, np.float32), time_stride=2)
    assert finer.ctc(torch.randn(1, 160, 80)).shape[1] == 80
