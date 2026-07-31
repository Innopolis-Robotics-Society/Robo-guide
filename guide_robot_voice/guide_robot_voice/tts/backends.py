"""Бэкенды синтеза речи за общим интерфейсом.

Абстракция нужна по трём причинам, и только третья очевидна.

1. Выбор Piper vs Silero для русского не сделан и делается ушами, а не
   бенчмарком. Silero на русском обычно звучит естественнее при сравнимой
   цене; менять это решение не должно означать переписывание ноды.

2. NullBackend делает весь TTS-путь тестируемым в CI без моделей и без
   звуковой карты. Он же -- измерительный инструмент: генерирует непрерывный
   тон известной частоты, по которому t_stop на железе измеряется внешним
   микрофоном однозначно, без вопросов о синхронизации часов.

3. Тёплый процесс. Загрузка модели -- секунды, и она обязана произойти
   в on_configure, а не при первом Say. Разогревочная фраза после загрузки
   прогоняет ленивую инициализацию и первый прогон JIT, иначе первая реплика
   экскурсии стабильно приходит с лишней задержкой.
"""

from __future__ import annotations

import math
import pathlib
from collections.abc import Iterator
from typing import Protocol

import numpy as np

__all__ = ["NullBackend", "PiperBackend", "SileroBackend", "TtsBackend", "make_backend"]


class TtsBackend(Protocol):
    """Синтезатор речи."""

    sample_rate: int

    def load(self) -> None:
        """Загрузить модель. Вызывается в on_configure, может быть медленным."""

    def warmup(self) -> None:
        """Прогнать разогревочную фразу. Результат никуда не выводится."""

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Выдать PCM int16 моно кадрами. Генератор, а не список.

        Реализация обязана отдавать кадры по мере готовности: вызывающий
        проверяет epoch между кадрами и прекращает синтез при отмене.
        Возврат целого массива в конце лишает его этой возможности.
        """

    def close(self) -> None:
        """Освободить модель."""


class NullBackend:
    """Детерминированный синтез без модели: тон вместо речи.

    Длительность пропорциональна длине текста, поэтому тайминги в тестах
    воспроизводимы. Тон непрерывный и громкий -- на осциллограмме внешней
    записи момент обрыва виден с точностью до периода.
    """

    def __init__(
        self,
        sample_rate: int = 22050,
        chars_per_second: float = 14.0,
        frequency: float = 440.0,
        block_ms: int = 20,
        amplitude: float = 0.25,
    ) -> None:
        """Создать генератор тона."""
        self.sample_rate = sample_rate
        self._chars_per_second = chars_per_second
        self._frequency = frequency
        self._block = int(sample_rate * block_ms / 1000)
        self._amplitude = amplitude

    def load(self) -> None:
        """Ничего не загружает."""

    def warmup(self) -> None:
        """Ничего не разогревает."""

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Выдать тон длительностью, пропорциональной длине текста."""
        total = max(1, int(self.sample_rate * len(text) / self._chars_per_second))
        peak = int(self._amplitude * 32767)
        phase = 0.0
        step = 2.0 * math.pi * self._frequency / self.sample_rate
        emitted = 0
        while emitted < total:
            count = min(self._block, total - emitted)
            index = np.arange(count, dtype=np.float64)
            block = np.sin(phase + step * index) * peak
            phase = (phase + step * count) % (2.0 * math.pi)
            emitted += count
            yield block.astype(np.int16)

    def close(self) -> None:
        """Ничего не освобождает."""


class PiperBackend:
    """Piper через библиотеку.

    API менялся. В piper1-gpl (текущий) потоковый метод -- synthesize(),
    выдающий чанки с audio_int16_bytes и sample_rate. В rhasspy/piper 1.2
    это был synthesize_stream_raw(), отдававший голые байты. Поддержаны оба:
    на роботе может оказаться любая версия, а падать на импорте из-за этого
    -- худший из возможных способов узнать.
    """

    def __init__(self, model_path: str, config_path: str = "", speaker_id: int = 0) -> None:
        """Запомнить пути. Модель загружается в load()."""
        self._model_path = model_path
        self._config_path = config_path
        self._speaker_id = speaker_id
        self._voice: object | None = None
        self._streaming: str = ""
        self.sample_rate = 22050

    def load(self) -> None:
        """Загрузить модель Piper и определить доступный API."""
        from piper import PiperVoice

        if self._config_path:
            self._voice = PiperVoice.load(self._model_path, config_path=self._config_path)
        else:
            self._voice = PiperVoice.load(self._model_path)

        config = getattr(self._voice, "config", None)
        rate = getattr(config, "sample_rate", None)
        if isinstance(rate, int):
            self.sample_rate = rate

        if hasattr(self._voice, "synthesize"):
            self._streaming = "synthesize"
        elif hasattr(self._voice, "synthesize_stream_raw"):
            self._streaming = "stream_raw"
        else:
            raise RuntimeError(
                "у PiperVoice нет ни synthesize(), ни synthesize_stream_raw(): "
                "неизвестная версия piper-tts"
            )

    def _check_files(self) -> None:
        """Проверить наличие обоих файлов голоса до попытки загрузки.

        Piper сообщает об отсутствии только .onnx.json, и сообщение выглядит
        так, будто модель нашлась, а конфига нет. На деле обычно нет обоих,
        и настоящая причина -- неверный путь целиком.
        """
        model = pathlib.Path(self._model_path)
        if not self._model_path:
            raise FileNotFoundError(
                "параметр model_path не задан. Скачать голос: "
                "python3 -m piper.download_voices ru_RU-irina-medium"
            )
        config = (
            pathlib.Path(self._config_path)
            if self._config_path
            else model.with_suffix(model.suffix + ".json")
        )
        missing = [str(path) for path in (model, config) if not path.exists()]
        if missing:
            hint = ""
            if not model.parent.exists():
                hint = f"\nКаталога {model.parent} не существует."
            raise FileNotFoundError(
                "не найдены файлы голоса Piper: " + ", ".join(missing) + hint + "\n"
                "download_voices кладёт их в текущий каталог; в model_path "
                "нужен абсолютный путь к .onnx, рядом должен лежать .onnx.json"
            )

    def warmup(self) -> None:
        """Снять стоимость первого прогона."""
        for _ in self.synthesize("Система готова."):
            pass

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Выдать PCM кадрами по мере синтеза."""
        if self._voice is None:
            raise RuntimeError("PiperBackend.load() не вызван")
        del voice

        if self._streaming == "synthesize":
            for chunk in self._voice.synthesize(text):  # type: ignore[attr-defined]
                rate = getattr(chunk, "sample_rate", None)
                if isinstance(rate, int) and rate != self.sample_rate:
                    self.sample_rate = rate
                yield np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            return

        stream = self._voice.synthesize_stream_raw(  # type: ignore[attr-defined]
            text, speaker_id=self._speaker_id
        )
        for raw in stream:
            yield np.frombuffer(raw, dtype=np.int16)

    def close(self) -> None:
        """Отпустить модель."""
        self._voice = None


class SileroBackend:
    """Silero TTS. На русском обычно естественнее Piper при сравнимой цене.

    Отдаёт весь тензор целиком, поэтому нарезка на кадры делается здесь.
    Это ухудшает отзывчивость на длинных клаузах -- ещё одна причина
    держать max_chars чанкера умеренным.
    """

    def __init__(
        self,
        model_path: str,
        speaker: str = "xenia",
        sample_rate: int = 24000,
        block_ms: int = 20,
        device: str = "cpu",
    ) -> None:
        """Запомнить параметры. Модель загружается в load()."""
        self._model_path = model_path
        self._speaker = speaker
        self.sample_rate = sample_rate
        self._block = int(sample_rate * block_ms / 1000)
        self._device = device
        self._model: object | None = None

    def load(self) -> None:
        """Загрузить torch-модель."""
        import torch

        self._model = torch.package.PackageImporter(self._model_path).load_pickle(
            "tts_models", "model"
        )
        self._model.to(torch.device(self._device))  # type: ignore[attr-defined]

    def warmup(self) -> None:
        """Прогнать короткую фразу."""
        for _ in self.synthesize("Система готова."):
            pass

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Синтезировать и отдать кадрами."""
        if self._model is None:
            raise RuntimeError("SileroBackend.load() не вызван")
        audio = self._model.apply_tts(  # type: ignore[attr-defined]
            text=text,
            speaker=voice or self._speaker,
            sample_rate=self.sample_rate,
        )
        pcm = (audio.numpy() * 32767).astype(np.int16)
        for start in range(0, pcm.shape[0], self._block):
            yield pcm[start : start + self._block]

    def close(self) -> None:
        """Отпустить модель."""
        self._model = None


def make_backend(kind: str, **kwargs: object) -> TtsBackend:
    """Собрать бэкенд по имени из параметров ноды."""
    factories = {
        "null": NullBackend,
        "piper": PiperBackend,
        "silero": SileroBackend,
    }
    if kind not in factories:
        raise ValueError(
            f"неизвестный бэкенд TTS: {kind!r}, ожидается один из {sorted(factories)}"
        )
    return factories[kind](**kwargs)  # type: ignore[arg-type,return-value]
