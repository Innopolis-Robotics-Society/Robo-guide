"""Юниты на предзаписанные клаузы облачного голоса."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import numpy as np

from guide_robot_voice.lib.backends import YandexBackend
from guide_robot_voice.lib.prerecorded import PrerecordedBackend, read_wav, write_wav


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
