import numpy as np

from scripts.analyze_wakeword_session import bounded_decode
from scripts.compare_wakeword_session import words_with_times


class Analyser:
    def __init__(self):
        self.calls = []

    def decode(self, audio):
        self.calls.append(len(audio) / 16000)
        # Synthetic samples identify their absolute second, making overlaps observable.
        first = int(audio[0])
        tokens, times = [], []
        for second in range(first, first + len(audio) // 16000):
            tokens.extend([" ", "ф", "и", "р", "а", "я"])
            times.extend([second - first + offset for offset in (0, 0.1, 0.2, 0.3, 0.4, 0.5)])
        return {"text": "".join(tokens), "tokens": tokens, "timestamps": times, "hints": []}


def test_long_asr_chunks_are_bounded_without_duplicate_names_or_lost_timestamps():
    analyser = Analyser()
    audio = np.repeat(np.arange(60, dtype=np.int16), 16000)
    result = bounded_decode(analyser, audio)
    assert max(analyser.calls) <= 18
    words = words_with_times(result)
    assert len(words) == 60
    np.testing.assert_allclose([word["start"] for word in words], np.arange(60) + 0.1)
    np.testing.assert_allclose([word["end"] for word in words], np.arange(60) + 0.58)
    assert result["chunked"]


def test_short_asr_keeps_original_decoder_result_without_chunking():
    analyser = Analyser()
    result = bounded_decode(analyser, np.zeros(20 * 16000, np.int16))
    assert analyser.calls == [20]
    assert "chunked" not in result
