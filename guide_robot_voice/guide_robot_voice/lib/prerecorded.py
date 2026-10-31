"""Предзаписанные клаузы облачного голоса: экскурсия не зависит от сети.

Тексты экскурсии известны заранее (content/*.yaml, фиксированные фразы
mission), поэтому их можно один раз синтезировать тем же Yandex SpeechKit
и тем же голосом в WAV (scripts/prerecord_tts.py), а на экскурсии играть
с диска. Синтез идёт прямо в файл, без микрофона -- в записи нет звука
помещения, только сама речь.

Запись ищется по клаузе, а не по целой реплике Say: tts_node режет текст
чанкером и зовёт бэкенд поклаузно, и ровно на этом уровне держится отмена
и учёт spoken_chars. Скрипт режет тем же TextChunker с теми же
min_chars/max_clause_chars. Имя файла -- YandexBackend.prerecord_key:
смена голоса, амплуа или текста даёт промах, а не чужую запись.

Промах (ответ ЛЛМ, текст после правки без перезаписи, резюме с середины
чанка) -- обычный путь через обёрнутый бэкенд: облако, при его отказе
запасной Silero. Обратно в каталог живые реплики не пишутся: ответы ЛЛМ
содержат данные посетителей и копились бы без предела.
"""

from __future__ import annotations

import pathlib
import wave
from collections.abc import Callable, Iterator

import numpy as np

from guide_robot_voice.lib.backends import TtsBackend
from guide_robot_voice.lib.resampler import resample_int16

__all__ = ["PrerecordedBackend", "read_wav", "write_wav"]


def read_wav(path: pathlib.Path) -> tuple[np.ndarray, int]:
    """Прочитать моно int16 WAV -> (PCM, частота)."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:  # noqa: PLR2004 -- int16
            raise ValueError(
                f"{path.name}: ожидается моно 16 бит, а не "
                f"{wav.getnchannels()} кан. x {wav.getsampwidth() * 8} бит"
            )
        rate = wav.getframerate()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.int16)
    return pcm, rate


def write_wav(path: pathlib.Path, pcm: np.ndarray, sample_rate: int) -> None:
    """Записать моно int16 WAV атомарно: оборванный файл не выдаст себя за запись."""
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    tmp.replace(path)


class PrerecordedBackend:
    """Клауза из каталога <directory>/<key>.wav, если есть; иначе -- inner."""

    def __init__(
        self,
        inner: TtsBackend,
        key_fn: Callable[[str, str], str],
        directory: str,
        block_ms: int = 20,
        on_miss: Callable[[str], None] | None = None,
    ) -> None:
        """Запомнить обёрнутый бэкенд. Каталог проверяется в load()."""
        self._inner = inner
        self._key_fn = key_fn
        self._directory = pathlib.Path(directory)
        self._block_ms = block_ms
        self._on_miss = on_miss
        self.available = 0
        """Сколько записей нашлось в каталоге при load()."""
        self.hits = 0
        self.misses = 0

    @property
    def sample_rate(self) -> int:
        """Частота обёрнутого бэкенда: записи пересчитываются к ней."""
        return self._inner.sample_rate

    @property
    def degraded_reason(self) -> str:
        """Причина перехода облака на запасной голос (для /diagnostics)."""
        return str(getattr(self._inner, "degraded_reason", ""))

    def load(self) -> None:
        """Загрузить обёрнутый бэкенд и пересчитать записи в каталоге."""
        self._inner.load()
        if self._directory.is_dir():
            self.available = sum(1 for _ in self._directory.glob("*.wav"))

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Отдать запись кадрами по block_ms или синтезировать через inner."""
        pcm = self._lookup(text, voice)
        if pcm is None:
            self.misses += 1
            if self._on_miss is not None:
                self._on_miss(text)
            yield from self._inner.synthesize(text, voice)
            return
        self.hits += 1
        block = max(1, int(self.sample_rate * self._block_ms / 1000))
        for offset in range(0, pcm.size, block):
            yield pcm[offset : offset + block]

    def _lookup(self, text: str, voice: str) -> np.ndarray | None:
        """PCM записи на частоте бэкенда или None, если записи нет."""
        path = self._directory / f"{self._key_fn(text, voice)}.wav"
        if not path.is_file():
            return None
        pcm, rate = read_wav(path)
        return resample_int16(pcm, rate, self.sample_rate)

    def close(self) -> None:
        """Отпустить обёрнутый бэкенд."""
        self._inner.close()
