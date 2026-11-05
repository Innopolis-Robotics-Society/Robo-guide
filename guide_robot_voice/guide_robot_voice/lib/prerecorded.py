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

LiveTTS от раза к разу звучит по-разному: тот же текст иногда выходит с
выкриком на 7-10 дБ громче остальной фразы и почти в клиппинг. Поэтому
скрипт меряет каждый дубль (measure_loudness), переписывает дубли с
выкриком и выравнивает громкость клауз между собой (level_to).
"""

from __future__ import annotations

import math
import pathlib
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np

from guide_robot_voice.lib.backends import TtsBackend
from guide_robot_voice.lib.resampler import resample_int16

__all__ = [
    "Loudness",
    "PrerecordedBackend",
    "level_to",
    "measure_loudness",
    "read_wav",
    "write_wav",
]

_FULL_SCALE = 32768.0
# Окна тише этого -- пауза, а не речь: в медиану уровня не идут.
_SILENCE_DB = -45.0


@dataclass(frozen=True)
class Loudness:
    """Громкость клаузы в дБ полной шкалы."""

    level_db: float
    """Медиана RMS по окнам с речью -- «обычная» громкость фразы."""
    burst_db: float
    """Насколько самое громкое окно громче level_db: выкрик."""
    peak_db: float


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-9) / _FULL_SCALE)


def measure_loudness(
    pcm: np.ndarray, sample_rate: int, window_s: float = 0.4, hop_s: float = 0.1
) -> Loudness:
    """Уровень, выкрик и пик клаузы по RMS скользящих окон window_s."""
    x = np.asarray(pcm, dtype=np.float64)
    if x.size == 0:
        return Loudness(level_db=_db(0.0), burst_db=0.0, peak_db=_db(0.0))
    window = max(1, min(x.size, int(window_s * sample_rate)))
    hop = max(1, int(hop_s * sample_rate))
    rms = np.array(
        [np.sqrt(np.mean(x[i : i + window] ** 2)) for i in range(0, x.size - window + 1, hop)]
    )
    rms_db = np.array([_db(v) for v in rms])
    voiced = rms_db[rms_db > _SILENCE_DB]
    level = float(np.median(voiced)) if voiced.size else float(rms_db.max())
    return Loudness(
        level_db=level,
        burst_db=float(rms_db.max()) - level,
        peak_db=_db(float(np.abs(x).max())),
    )


def level_to(  # noqa: PLR0913 -- всё после target_db keyword-only
    pcm: np.ndarray,
    loudness: Loudness,
    target_db: float,
    *,
    peak_ceiling_db: float = -1.0,
    max_limit_db: float = 0.0,
    sample_rate: int = 48000,
) -> tuple[np.ndarray, float]:
    """Привести уровень клаузы к target_db одним усилением, пик не выше peak_ceiling_db.

    Усиление общее на всю клаузу -- форма речи не меняется, только громкость.
    max_limit_db > 0 разрешает поднять уровень выше, чем пускает пик, а
    отдельные пики плавно прижать лимитером не больше чем на max_limit_db
    (без этого громкую фразу не поднять: речь пиковее своего уровня на
    15-17 дБ). Возвращает (PCM, усиление уровня в дБ).
    """
    headroom_db = peak_ceiling_db - loudness.peak_db
    gain_db = min(target_db - loudness.level_db, headroom_db + max(0.0, max_limit_db))
    scaled = np.asarray(pcm, dtype=np.float64) * 10.0 ** (gain_db / 20.0)
    if gain_db > headroom_db:
        ceiling = _FULL_SCALE * 10.0 ** (peak_ceiling_db / 20.0)
        scaled *= _limiter_gain(scaled, ceiling, sample_rate)
    return np.clip(np.round(scaled), -32768, 32767).astype(np.int16), gain_db


def _limiter_gain(
    x: np.ndarray, ceiling: float, sample_rate: int, smooth_s: float = 0.01
) -> np.ndarray:
    """Плавное усиление <= 1, после которого |x| не выше ceiling.

    Минимум нужного усиления по окну 2W, затем сглаживание окном W: в
    сглаживание попадают только значения, чьи окна накрывают сам отсчёт,
    поэтому пик гарантированно прижат, а усиление меняется за ~smooth_s,
    без щелчков.
    """
    need = np.minimum(1.0, ceiling / np.maximum(np.abs(x), 1e-9))
    half = max(1, int(smooth_s * sample_rate / 2))
    padded = np.pad(need, (2 * half, 2 * half), constant_values=1.0)
    held = np.lib.stride_tricks.sliding_window_view(padded, 4 * half + 1).min(axis=1)
    kernel = np.hanning(2 * half + 3)[1:-1]  # без нулевых концов
    held = np.pad(held, (half, half), constant_values=1.0)
    smooth = np.convolve(held, kernel / kernel.sum(), mode="valid")
    return np.minimum(smooth, need)


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
