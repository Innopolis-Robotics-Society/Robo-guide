import numpy as np
import pytest

from scripts.wakeword_stretch import time_stretch


@pytest.mark.parametrize("rate", [0.8, 1.3, 1.6, 1.9])
def test_duration_changes_while_tone_frequency_stays_constant(rate):
    audio = np.rint(10000 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.int16)
    stretched = time_stretch(audio, rate)
    assert len(stretched) == round(len(audio) / rate)
    assert stretched.dtype == np.int16
    middle = stretched[len(stretched) // 4 : 3 * len(stretched) // 4]
    power = np.abs(np.fft.rfft(middle.astype(float) * np.hanning(len(middle))))
    peak = np.fft.rfftfreq(len(middle), 1 / 16000)[np.argmax(power)]
    assert abs(peak - 440) < 5
    assert np.sqrt(np.mean(middle.astype(float) ** 2)) > 4000


def test_both_ends_of_a_two_part_signal_survive_acceleration():
    time = np.arange(16000) / 16000
    audio = np.rint(8000 * np.sin(2 * np.pi * np.where(time < 0.5, 300, 900) * time)).astype(
        np.int16
    )
    result = time_stretch(audio, 1.6)
    for part, frequency in ((result[500:4000], 300), (result[6500:9500], 900)):
        bins = np.fft.rfftfreq(len(part), 1 / 16000)
        peak = bins[np.argmax(np.abs(np.fft.rfft(part.astype(float) * np.hanning(len(part)))))]
        assert abs(peak - frequency) < 10


def test_identity_returns_independent_unchanged_pcm_and_invalid_inputs_fail():
    audio = np.arange(1000, dtype=np.int16)
    result = time_stretch(audio, 1)
    np.testing.assert_array_equal(result, audio)
    assert not np.shares_memory(result, audio)
    for rate in (0, float("nan"), 3):
        with pytest.raises(ValueError):
            time_stretch(audio, rate)
