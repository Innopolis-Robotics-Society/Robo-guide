"""Синтезатор речи (Piper) за интерфейсом, тестируемым без модели.

Абстракция нужна по двум причинам.

1. NullBackend делает весь путь tts_node тестируемым в CI и на роботе без
   модели и без звуковой карты: он генерирует непрерывный тон известной
   частоты, по которому t_stop (см. design §4) измеряется однозначно,
   без вопросов о синхронизации часов между процессом синтеза и внешним
   микрофоном.

2. Тёплый процесс. Загрузка модели -- секунды, и она обязана произойти
   в on_configure, а не при первом Say. Разогревочная фраза после загрузки
   прогоняет ленивую инициализацию ONNX-графа, иначе первая реальная
   реплика экскурсии стабильно приходит с лишней задержкой.

Голос зафиксирован дизайном -- Piper ru_RU-irina-medium (design §3.5).
Импорт piper отложен внутрь load(): модуль обязан импортироваться в CI,
где ни модели, ни самого piper-tts может не быть.
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
from collections.abc import Iterator
from typing import Protocol

import numpy as np

_logger = logging.getLogger(__name__)

__all__ = ["NullBackend", "PiperBackend", "TtsBackend", "make_backend"]


class TtsBackend(Protocol):
    """Синтезатор речи."""

    sample_rate: int

    def load(self) -> None:
        """Загрузить модель. Вызывается в on_configure, может быть медленным."""

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

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Выдать тон длительностью, пропорциональной длине текста."""
        del voice
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
    """Piper через библиотеку piper-tts.

    API менялся между версиями. В piper1-gpl (текущий) потоковый метод --
    synthesize(text, syn_config=SynthesisConfig(...)), выдающий чанки
    с audio_int16_bytes и sample_rate. В rhasspy/piper 1.2 это был
    synthesize_stream_raw(text, speaker_id=..., length_scale=...), отдававший
    голые байты. Поддержаны оба: на роботе может оказаться любая версия,
    а падать на импорте из-за этого -- худший из возможных способов узнать.
    """

    def __init__(
        self,
        model_path: str,
        config_path: str = "",
        speaker_id: int = 0,
        length_scale: float = 1.0,
    ) -> None:
        """Запомнить параметры. Модель загружается в load()."""
        self._model_path = model_path
        self._config_path = config_path
        self._speaker_id = speaker_id
        self._length_scale = length_scale
        self._voice: object | None = None
        self._streaming: str = ""
        self._syn_config_cls: type | None = None
        self.sample_rate = 22050

    def load(self) -> None:
        """Загрузить модель Piper и определить доступный API.

        Собирает InferenceSession вручную, а не через PiperVoice.load(), --
        тому негде передать SessionOptions, а их обязательно нужно менять.

        ПОЧЕМУ. Эта модель -- VITS со стохастическим duration predictor:
        длина внутренних тензоров после него по определению зависит от
        случайного шума и меняется от вызова к вызову, это не баг, а как
        работает архитектура. Документация onnxruntime на enable_mem_pattern
        прямо требует одинаковых форм входов между вызовами сессии; здесь
        это условие нарушено самой моделью. На реальном железе это дало
        ровно предсказанную картину: Reshape/GatherElements валится
        "out of range"/"dimension zero" через десяток успешных вызовов
        той же самой фразы -- план памяти, закешированный под форму
        первого вызова, не подходит следующему. intra_op_num_threads=1 --
        на случай гонки в общем тред-пуле сессии; throughput здесь не
        нужен, один инференс в ~15 раз быстрее реального времени и без
        параллелизма.

        Если конструктор PiperVoice в установленной версии piper-tts не
        совпадает (старый rhasspy/piper), откатываемся на PiperVoice.load()
        без этих опций -- рабочий бэкенд с риском редкого сбоя лучше отказа
        загрузки вовсе.
        """
        self._check_files()
        from piper import PiperVoice

        try:
            self._voice = self._load_with_safe_session_options()
        except Exception as error:
            _logger.warning(
                "не удалось собрать сессию onnxruntime с безопасными опциями "
                "(%s), откатываюсь на PiperVoice.load() по умолчанию",
                error,
            )
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
            try:
                from piper import SynthesisConfig

                self._syn_config_cls = SynthesisConfig
            except ImportError:
                self._syn_config_cls = None
        elif hasattr(self._voice, "synthesize_stream_raw"):
            self._streaming = "stream_raw"
        else:
            raise RuntimeError(
                "у PiperVoice нет ни synthesize(), ни synthesize_stream_raw(): "
                "неизвестная версия piper-tts"
            )

    def _load_with_safe_session_options(self) -> object:
        """Собрать PiperVoice с отключённым mem_pattern/arena, однопоточно."""
        import onnxruntime
        from piper import PiperConfig, PiperVoice

        config_path = self._config_path or f"{self._model_path}.json"
        with pathlib.Path(config_path).open(encoding="utf-8") as config_file:
            config_dict = json.load(config_file)

        sess_options = onnxruntime.SessionOptions()
        sess_options.enable_mem_pattern = False
        sess_options.enable_cpu_mem_arena = False
        sess_options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        sess_options.intra_op_num_threads = 1

        session = onnxruntime.InferenceSession(
            self._model_path,
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        return PiperVoice(session=session, config=PiperConfig.from_dict(config_dict))

    def _check_files(self) -> None:
        """Проверить наличие обоих файлов голоса до попытки загрузки.

        Piper сообщает об отсутствии только .onnx.json, и сообщение выглядит
        так, будто модель нашлась, а конфига нет. На деле обычно нет обоих,
        и настоящая причина -- неверный путь целиком.
        """
        if not self._model_path:
            raise FileNotFoundError(
                "параметр model_path не задан. Скачать голос: "
                "python3 -m piper.download_voices ru_RU-irina-medium"
            )
        model = pathlib.Path(self._model_path)
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

    def _resolve_speaker_id(self, voice: str) -> int:
        """Заявка Say.voice переопределяет speaker_id, если это число.

        Мультиголосых моделей в текущей конфигурации нет, но поле voice
        в контракте Say.action уже существует -- лучше поддержать его
        сейчас, чем менять сигнатуру позже.
        """
        if voice and voice.lstrip("-").isdigit():
            return int(voice)
        return self._speaker_id

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Выдать PCM кадрами по мере синтеза."""
        if self._voice is None:
            raise RuntimeError("PiperBackend.load() не вызван")
        speaker_id = self._resolve_speaker_id(voice)

        if self._streaming == "synthesize":
            syn_config = None
            if self._syn_config_cls is not None:
                syn_config = self._syn_config_cls(
                    speaker_id=speaker_id, length_scale=self._length_scale
                )
            for chunk in self._voice.synthesize(text, syn_config=syn_config):  # type: ignore[attr-defined]
                rate = getattr(chunk, "sample_rate", None)
                if isinstance(rate, int) and rate != self.sample_rate:
                    self.sample_rate = rate
                yield np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            return

        stream = self._voice.synthesize_stream_raw(  # type: ignore[attr-defined]
            text, speaker_id=speaker_id, length_scale=self._length_scale
        )
        for raw in stream:
            yield np.frombuffer(raw, dtype=np.int16)

    def close(self) -> None:
        """Отпустить модель."""
        self._voice = None


def make_backend(kind: str, **kwargs: object) -> TtsBackend:
    """Собрать бэкенд по имени из параметров ноды."""
    factories = {"null": NullBackend, "piper": PiperBackend}
    if kind not in factories:
        raise ValueError(
            f"неизвестный бэкенд TTS: {kind!r}, ожидается один из {sorted(factories)}"
        )
    return factories[kind](**kwargs)  # type: ignore[arg-type,return-value]
