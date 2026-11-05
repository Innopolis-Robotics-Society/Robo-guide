"""Юниты на предзаписанные клаузы облачного голоса."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import numpy as np

from guide_robot_voice.lib.backends import YandexBackend
from guide_robot_voice.lib.prerecorded import (
    PrerecordedBackend,
    level_to,
    measure_loudness,
    read_wav,
    write_wav,
)


class _CountingBackend:
    """Бэкенд-заглушка: считает вызовы, отдаёт тон 0.1 с."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.calls: list[str] = []
        self.loaded = False
        self.degraded_reason = ""

    def load(self) -> None:
        self.loaded = True

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        del voice
        self.calls.append(text)
        yield np.full(self.sample_rate // 10, 1000, dtype=np.int16)

    def close(self) -> None:
        pass


def _key(text: str, voice: str) -> str:
    return f"{voice or 'default'}-{len(text)}"


def test_wav_roundtrip(tmp_path: pathlib.Path) -> None:
    pcm = (np.sin(np.arange(4800) / 10) * 10000).astype(np.int16)
    write_wav(tmp_path / "a.wav", pcm, 48000)
    restored, rate = read_wav(tmp_path / "a.wav")
    assert rate == 48000
    np.testing.assert_array_equal(restored, pcm)
    assert not list(tmp_path.glob("*.part"))


def test_hit_plays_file_resampled_without_inner(tmp_path: pathlib.Path) -> None:
    """Запись 48 кГц играется на 16 кГц бэкенда, облако не трогается."""
    write_wav(tmp_path / f"{_key('Привет.', '')}.wav", np.full(48000, 500, np.int16), 48000)
    inner = _CountingBackend(sample_rate=16000)
    backend = PrerecordedBackend(inner, _key, str(tmp_path), block_ms=20)
    backend.load()

    blocks = list(backend.synthesize("Привет."))

    assert inner.calls == []
    assert backend.available == 1
    assert (backend.hits, backend.misses) == (1, 0)
    assert all(block.size <= 320 for block in blocks)
    assert abs(sum(block.size for block in blocks) - 16000) <= 2


def test_miss_goes_to_inner_and_reports(tmp_path: pathlib.Path) -> None:
    inner = _CountingBackend()
    missed: list[str] = []
    backend = PrerecordedBackend(inner, _key, str(tmp_path), on_miss=missed.append)
    backend.load()

    blocks = list(backend.synthesize("Ответ ЛЛМ", "vera:casual"))

    assert inner.loaded
    assert inner.calls == ["Ответ ЛЛМ"]
    assert missed == ["Ответ ЛЛМ"]
    assert (backend.hits, backend.misses) == (0, 1)
    assert blocks
    assert not list(tmp_path.glob("*.wav")), "живые реплики не пишутся в каталог"


def test_missing_directory_is_not_fatal(tmp_path: pathlib.Path) -> None:
    inner = _CountingBackend()
    backend = PrerecordedBackend(inner, _key, str(tmp_path / "nope"))
    backend.load()
    assert backend.available == 0
    assert list(backend.synthesize("текст"))
    assert inner.calls == ["текст"]


def test_degraded_reason_comes_from_inner(tmp_path: pathlib.Path) -> None:
    inner = _CountingBackend()
    backend = PrerecordedBackend(inner, _key, str(tmp_path))
    inner.degraded_reason = "нет денег"
    assert backend.degraded_reason == "нет денег"


def test_yandex_prerecord_key_ignores_rate_but_not_voice() -> None:
    at_16k = YandexBackend(voice="vera", role="casual", model="livetts", sample_rate=16000)
    at_48k = YandexBackend(voice="vera", role="casual", model="livetts", sample_rate=48000)
    other_role = YandexBackend(voice="vera", role="narrator", model="livetts")
    text = "Меня зовут Фирая."

    assert at_16k.prerecord_key(text) == at_48k.prerecord_key(text)
    assert at_16k.prerecord_key(text) != other_role.prerecord_key(text)
    assert at_16k.prerecord_key(text) != at_16k.prerecord_key(text, "masha")
    assert at_16k.prerecord_key(text) != at_16k.prerecord_key("Меня зовут Фирая!")
    # Пробелы схлопывает strip_yandex_markup -- тот же звук, тот же ключ.
    assert at_16k.prerecord_key(text) == at_16k.prerecord_key("Меня  зовут Фирая.")


def _tone(seconds: float, amplitude: float, rate: int = 16000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return amplitude * np.sin(2 * np.pi * 220 * t)


def test_loudness_finds_burst_and_ignores_pauses() -> None:
    rate = 16000
    calm = _tone(2.0, 3000)
    pause = np.zeros(rate)
    shout = _tone(0.5, 12000)  # +12 дБ к ровной речи
    pcm = np.concatenate([calm, pause, shout, calm]).astype(np.int16)

    loudness = measure_loudness(pcm, rate)

    expected_level = 20 * np.log10(3000 / np.sqrt(2) / 32768)
    assert abs(loudness.level_db - expected_level) < 0.5
    assert 10.0 < loudness.burst_db < 13.0
    assert abs(loudness.peak_db - 20 * np.log10(12000 / 32768)) < 0.1


def test_even_speech_has_no_burst() -> None:
    loudness = measure_loudness(_tone(3.0, 5000).astype(np.int16), 16000)
    assert loudness.burst_db < 0.5


def test_level_to_target_and_peak_ceiling() -> None:
    rate = 16000
    quiet = _tone(2.0, 1000).astype(np.int16)
    leveled, gain = level_to(quiet, measure_loudness(quiet, rate), target_db=-20.0)
    assert abs(measure_loudness(leveled, rate).level_db + 20.0) < 0.3
    assert gain > 0

    # Пик упёрся бы в клиппинг -- усиление ограничено потолком, а не целью.
    spiky = np.concatenate([_tone(2.0, 1000), _tone(0.2, 30000)]).astype(np.int16)
    leveled, _ = level_to(spiky, measure_loudness(spiky, rate), -20.0, peak_ceiling_db=-1.0)
    assert measure_loudness(leveled, rate).peak_db <= -0.9


def test_limiter_lifts_level_past_peak_without_clipping() -> None:
    rate = 16000
    spiky = np.concatenate([_tone(2.0, 1000), _tone(0.2, 30000), _tone(2.0, 1000)]).astype(
        np.int16
    )
    before = measure_loudness(spiky, rate)
    target = before.level_db + 6.0

    leveled, gain = level_to(spiky, before, target, max_limit_db=4.0, sample_rate=rate)
    after = measure_loudness(leveled, rate)

    # Без лимитера усиление упёрлось бы в запас до потолка; лимитер даёт ещё 4 дБ.
    headroom = -1.0 - before.peak_db
    assert abs(gain - (headroom + 4.0)) < 0.01
    assert abs(after.level_db - (before.level_db + gain)) < 0.3
    assert after.peak_db <= -0.9
    # Тихая речь вдали от пика не тронута лимитером.
    assert np.allclose(leveled[:rate], spiky[:rate] * 10 ** (gain / 20), atol=1)
