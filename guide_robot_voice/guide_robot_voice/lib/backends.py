"""Синтезаторы речи (Silero / Piper) за интерфейсом, тестируемым без модели.

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

Голос по умолчанию -- Silero TTS v5 `xenia` (v5_ru.pt). Piper оставлен
как запасной бэкенд. Yandex SpeechKit v3 -- облачный эмоциональный голос
(амплуа), ходит в сеть на каждую клаузу. Импорт torch/piper/requests
отложен внутрь load(): модуль обязан импортироваться в CI без моделей.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import math
import os
import pathlib
import re
import time
from collections.abc import Iterator
from http import HTTPStatus
from typing import Protocol

import numpy as np

from guide_robot_voice.lib.resampler import resample_int16

_logger = logging.getLogger(__name__)

__all__ = [
    "FallbackBackend",
    "NullBackend",
    "PiperBackend",
    "SileroBackend",
    "TtsBackend",
    "TtsServiceError",
    "YandexBackend",
    "build_silero_ssml",
    "make_backend",
    "parse_yandex_voice",
    "strip_yandex_markup",
    "trim_trailing_silence",
]

_SILERO_SAMPLE_RATES = frozenset({8000, 24000, 48000})


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
                "(%s), откатываюсь на PiperVoice.load(use_cuda=True)",
                error,
            )
            try:
                if self._config_path:
                    self._voice = PiperVoice.load(
                        self._model_path, config_path=self._config_path, use_cuda=True
                    )
                else:
                    self._voice = PiperVoice.load(self._model_path, use_cuda=True)
            except Exception as cuda_error:
                _logger.warning(
                    "не удалось запустить PiperVoice с use_cuda=True (%s), использую CPU",
                    cuda_error,
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
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        try:
            return PiperVoice(session=session, config=PiperConfig.from_dict(config_dict))
        except TypeError:
            return PiperVoice.load(self._model_path, config_path=config_path, use_cuda=True)

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


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TRAILING_SILENCE_THRESHOLD = 200  # LSB; хвост Silero -- не нули, а шум ~единиц LSB


def build_silero_ssml(text: str, rate: str = "100%", sentence_pause_ms: int = 0) -> str | None:
    """Собрать SSML для apply_tts(ssml_text=...) или None, если он не нужен.

    Silero v5 понимает <prosody rate> (проценты и fast/x-fast) и
    <break time>. Сама модель ставит на точке паузу ~350-430 мс; явный
    <break> между предложениями заменяет её на заданную (измерено на
    роботе 15.09.2026: 100 мс убирает все паузы >=150 мс). Текст
    экранируется: ответ ЛЛМ с «&» или «<» иначе ломает разбор SSML.
    """
    rate = rate.strip() or "100%"
    if rate == "100%" and sentence_pause_ms <= 0:
        return None
    if sentence_pause_ms > 0:
        parts = [p for p in _SENTENCE_SPLIT.split(text.strip()) if p]
        body = f'<break time="{int(sentence_pause_ms)}ms"/>'.join(html.escape(p) for p in parts)
    else:
        body = html.escape(text)
    if rate != "100%":
        body = f'<prosody rate="{html.escape(rate, quote=True)}">{body}</prosody>'
    return f"<speak>{body}</speak>"


def trim_trailing_silence(pcm: np.ndarray, keep_ms: int, sample_rate: int) -> np.ndarray:
    """Оставить в конце не больше keep_ms тишины (keep_ms < 0 -- не трогать).

    Каждая клауза Silero заканчивается ~230 мс тишины; на стыке клауз
    чанкера она складывается с паузой следующей. Обрезка -- по порогу
    |x| < 200 LSB, чтобы шум хвоста не считался речью.
    """
    if keep_ms < 0 or pcm.size == 0:
        return pcm
    loud = np.nonzero(np.abs(pcm.astype(np.int32)) >= _TRAILING_SILENCE_THRESHOLD)[0]
    if loud.size == 0:
        return pcm
    end = min(pcm.size, int(loud[-1]) + 1 + int(sample_rate * keep_ms / 1000))
    return pcm[:end]


class SileroBackend:
    """Silero TTS v5 через torch.package (v5_ru.pt, спикер xenia).

    apply_tts отдаёт всю фразу целиком -- режем на block_ms, чтобы
    tts_node мог проверить epoch между кадрами. Импорт torch только в
    load(): CI без CUDA/torch не должен падать на import backends.
    """

    def __init__(  # noqa: PLR0913 -- параметры ноды один к одному
        self,
        model_path: str,
        speaker: str = "xenia",
        sample_rate: int = 48000,
        block_ms: int = 20,
        rate: str = "100%",
        sentence_pause_ms: int = 0,
        trailing_silence_ms: int = -1,
    ) -> None:
        """Запомнить параметры. Модель загружается в load()."""
        if sample_rate not in _SILERO_SAMPLE_RATES:
            supported = ", ".join(str(rate) for rate in sorted(_SILERO_SAMPLE_RATES))
            raise ValueError(
                f"Silero TTS не поддерживает {sample_rate} Гц; допустимо: {supported}"
            )
        """Запомнить параметры. Модель загружается в load().

        rate -- <prosody rate> SSML ("100%" -- как есть); sentence_pause_ms --
        явная пауза между предложениями вместо модельной (0 -- модельная);
        trailing_silence_ms -- сколько тишины оставить в конце клаузы
        (-1 -- не обрезать). См. build_silero_ssml / trim_trailing_silence.
        """
        self._model_path = model_path
        self._speaker = speaker
        self.sample_rate = sample_rate
        self._block = max(1, int(sample_rate * block_ms / 1000))
        self._rate = rate
        self._sentence_pause_ms = sentence_pause_ms
        self._trailing_silence_ms = trailing_silence_ms
        self._model: object | None = None

    def load(self) -> None:
        """Загрузить torch.package и перенести на CUDA, если она есть."""
        path = pathlib.Path(self._model_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"не найден Silero TTS: {path}. Ожидается models/v5_ru.pt (git-lfs)."
            )
        import torch

        self._model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(device)  # type: ignore[union-attr]
        _logger.info("Silero TTS v5 на %s, спикер %s", device, self._speaker)

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Синтезировать фразу и отдать PCM int16 кадрами по block_ms."""
        if self._model is None:
            raise RuntimeError("SileroBackend.load() не вызван")
        speaker = voice if voice and not voice.lstrip("-").isdigit() else self._speaker
        ssml = build_silero_ssml(text, self._rate, self._sentence_pause_ms)
        if ssml is not None:
            audio = self._model.apply_tts(  # type: ignore[union-attr]
                ssml_text=ssml, speaker=speaker, sample_rate=self.sample_rate
            )
        else:
            audio = self._model.apply_tts(  # type: ignore[union-attr]
                text=text, speaker=speaker, sample_rate=self.sample_rate
            )
        pcm = trim_trailing_silence(
            _silero_to_int16(audio), self._trailing_silence_ms, self.sample_rate
        )
        offset = 0
        while offset < pcm.size:
            yield pcm[offset : offset + self._block]
            offset += self._block

    def close(self) -> None:
        """Отпустить модель."""
        self._model = None


def _silero_to_int16(audio: object) -> np.ndarray:
    """Тензор/массив Silero [-1, 1] -> int16 моно."""
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()  # type: ignore[union-attr]
    pcm = np.asarray(audio, dtype=np.float32).reshape(-1)
    return np.clip(np.round(pcm * 32767.0), -32768, 32767).astype(np.int16)


_YANDEX_TTS_URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"
# Разметка Yandex TTS: «+» -- ударение, «**слово**» -- акцент, «sil<[t]>»,
# «<[small]>» -- паузы, «[[...]]» -- фонемы. В тексте ЛЛМ эти символы --
# не разметка («C++», markdown), и прочитаны они должны быть как текст.
_YANDEX_MARKUP_RE = re.compile(r"[*\[\]<>]")
_SPACES_RE = re.compile(r"\s+")


class TtsServiceError(RuntimeError):
    """Облачный синтез отказал. persistent -- ключ, права или деньги.

    persistent=True: HTTP 4xx (кроме 408/429) и gRPC PERMISSION_DENIED /
    UNAUTHENTICATED -- сам не пройдёт, повторять каждую клаузу бессмысленно.
    Иначе (сеть, таймаут, 5xx, 429) -- сбой временный.
    """

    def __init__(self, message: str, *, persistent: bool) -> None:
        """Запомнить, временный ли сбой."""
        super().__init__(message)
        self.persistent = persistent


_TRANSIENT_HTTP = frozenset({408, 429})
_PERSISTENT_GRPC = frozenset({7, 16})  # PERMISSION_DENIED, UNAUTHENTICATED


def strip_yandex_markup(text: str) -> str:
    """Убрать из текста всё, что SpeechKit примет за разметку."""
    text = _YANDEX_MARKUP_RE.sub(" ", text.replace("+", " плюс "))
    return _SPACES_RE.sub(" ", text).strip()


def parse_yandex_voice(voice: str, default_voice: str, default_role: str) -> tuple[str, str]:
    """Say.voice -> (голос, амплуа).

    Формат «голос:амплуа»; «:амплуа» -- амплуа для голоса по умолчанию.
    Голос без амплуа берёт амплуа голоса по умолчанию у самого SpeechKit,
    а не default_role: у другого голоса такого амплуа может не быть.
    Число (speaker_id Piper) -- не голос Яндекса, игнорируется.
    """
    voice = voice.strip()
    if not voice or voice.lstrip("-").isdigit():
        return default_voice, default_role
    name, _, role = voice.partition(":")
    name, role = name.strip(), role.strip()
    if not name:
        return default_voice, role or default_role
    return name, role


class YandexBackend:
    """Yandex SpeechKit API v3 (REST utteranceSynthesis), сырой PCM.

    Ответ -- поток JSON-сообщений с audioChunk.data (base64 PCM), кадры
    отдаются по мере прихода. Если вызывающий бросает генератор (отмена),
    HTTP-ответ закрывается в with -- запрос не висит до конца синтеза.

    Ключ -- API-ключ сервисного аккаунта с ролью ai.speechkit-tts.user,
    берётся из переменной окружения (в git не лежит). Заголовок
    x-data-logging-enabled: false -- Яндекс иначе логирует тексты
    реплик, а в ответах ЛЛМ бывают данные посетителей.
    """

    def __init__(  # noqa: PLR0913 -- параметры ноды один к одному
        self,
        voice: str = "dasha",
        role: str = "",
        model: str = "",
        speed: str = "",
        sample_rate: int = 48000,
        api_key_env: str = "YANDEX_TTS_API_KEY",
        folder_id: str = "",
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 10.0,
        block_ms: int = 20,
        url: str = _YANDEX_TTS_URL,
    ) -> None:
        """Запомнить параметры. Ключ читается и сессия создаётся в load().

        model -- "" (general) или "livetts"; у livetts нет speed в API v3.
        speed -- строка, как в API ("1.2"); пусто -- не передавать.
        """
        self._voice = voice
        self._role = role
        self._model = model
        self._speed = speed
        self.sample_rate = sample_rate
        self._api_key_env = api_key_env
        self._folder_id = folder_id
        self._timeout = (connect_timeout_s, read_timeout_s)
        self._block = max(1, int(sample_rate * block_ms / 1000))
        self._url = url
        self._session: object | None = None

    def load(self) -> None:
        """Прочитать ключ и открыть HTTP-сессию (keep-alive между клаузами)."""
        api_key = os.environ.get(self._api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"переменная окружения {self._api_key_env} пуста: положите API-ключ "
                "сервисного аккаунта SpeechKit в .env и пробросьте её в compose.yaml"
            )
        if self._model == "livetts" and self._speed:
            raise ValueError("livetts не поддерживает speed в API v3: оставьте пустым")
        import requests

        session = requests.Session()
        session.headers.update(
            {"Authorization": f"Api-Key {api_key}", "x-data-logging-enabled": "false"}
        )
        if self._folder_id:
            session.headers["x-folder-id"] = self._folder_id
        self._session = session
        _logger.info(
            "Yandex SpeechKit: модель %s, голос %s, амплуа %s, %d Гц",
            self._model or "general",
            self._voice,
            self._role or "по умолчанию",
            self.sample_rate,
        )

    def build_request(self, text: str, voice: str = "") -> dict[str, object]:
        """Тело запроса utteranceSynthesis -- отдельно, чтобы проверять без сети."""
        name, role = parse_yandex_voice(voice, self._voice, self._role)
        hints: list[dict[str, str]] = [{"voice": name}]
        if role:
            hints.append({"role": role})
        if self._speed:
            hints.append({"speed": self._speed})
        body: dict[str, object] = {
            "text": strip_yandex_markup(text),
            "hints": hints,
            "outputAudioSpec": {
                "rawAudio": {
                    "audioEncoding": "LINEAR16_PCM",
                    "sampleRateHertz": str(self.sample_rate),
                }
            },
        }
        if self._model:
            body["model"] = self._model
        return body

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Синтезировать фразу и отдавать PCM int16 кадрами по мере прихода."""
        if self._session is None:
            raise RuntimeError("YandexBackend.load() не вызван")
        body = self.build_request(text, voice)
        with self._session.post(  # type: ignore[attr-defined]
            self._url, json=body, stream=True, timeout=self._timeout
        ) as response:
            code = response.status_code
            if code != HTTPStatus.OK:
                raise TtsServiceError(
                    f"Yandex TTS HTTP {code}: {response.text[:300]}",
                    persistent=HTTPStatus.BAD_REQUEST <= code < HTTPStatus.INTERNAL_SERVER_ERROR
                    and code not in _TRANSIENT_HTTP,
                )
            carry = b""
            for message in _iter_json_messages(response.iter_content(chunk_size=None)):
                if "error" in message:
                    error = message["error"]
                    grpc_code = error.get("grpcCode") if isinstance(error, dict) else None
                    raise TtsServiceError(
                        f"Yandex TTS: {error}", persistent=grpc_code in _PERSISTENT_GRPC
                    )
                chunk = message.get("result", message).get("audioChunk") or {}
                data = chunk.get("data")
                if not data:
                    continue
                raw = carry + base64.b64decode(data)
                usable = len(raw) - len(raw) % 2
                carry = raw[usable:]
                pcm = np.frombuffer(raw[:usable], dtype="<i2").astype(np.int16)
                for offset in range(0, pcm.size, self._block):
                    yield pcm[offset : offset + self._block]

    def close(self) -> None:
        """Закрыть HTTP-сессию."""
        if self._session is not None:
            self._session.close()  # type: ignore[attr-defined]
            self._session = None


def _iter_json_messages(chunks: Iterator[bytes]) -> Iterator[dict]:
    """Разобрать поток подряд идущих JSON-объектов из произвольно нарезанных байтов.

    REST-шлюз отдаёт серверный стрим как объекты {"result": ...} через
    перевод строки; разделители (пробелы, «,», «[», «]») пропускаются,
    чтобы не зависеть от того, завернёт ли шлюз стрим в массив. UTF-8
    декодируется инкрементально: кириллица textChunk может разорваться
    на границе сетевых чанков.
    """
    import codecs

    decoder = json.JSONDecoder()
    text_decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    for chunk in chunks:
        buffer += text_decoder.decode(chunk)
        while True:
            buffer = buffer.lstrip(" \t\r\n,[]")
            if not buffer:
                break
            try:
                message, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break  # объект ещё не пришёл целиком
            if isinstance(message, dict):
                yield message
            buffer = buffer[end:]
    if buffer.lstrip(" \t\r\n,[]"):
        raise TtsServiceError(f"Yandex TTS: оборванный ответ: {buffer[:200]!r}", persistent=False)


class FallbackBackend:
    """Облачный бэкенд с локальной страховкой: робот не замолкает.

    Кончились деньги, отозван ключ, пропала сеть -- клауза синтезируется
    запасным (Silero), а основной пропускается до retry_s (временный сбой)
    или persistent_retry_s (TtsServiceError.persistent: ключ/права/деньги).
    После паузы основной пробуется снова -- пополнили баланс, вернулась
    сеть -- и голос возвращается сам, без перезапуска.

    Подмена только до первого кадра основного: если он отвалился посреди
    клаузы, часть звука уже у вызывающего, и дописать хвост другим голосом
    нельзя -- исключение уходит в tts_node._push_clause, повтор клаузы
    целиком придёт сюда же и уйдёт в запасной.

    Запасной получает пустой voice: Say.voice -- в формате облака
    («голос:амплуа»), Silero принял бы его за имя спикера. Его PCM
    пересчитывается к частоте основного: tts_node строит ресемплер и
    устройство под одну частоту бэкенда.
    """

    def __init__(  # noqa: PLR0913 -- параметры ноды один к одному
        self,
        primary: TtsBackend,
        fallback: TtsBackend,
        retry_s: float = 30.0,
        persistent_retry_s: float = 300.0,
        block_ms: int = 20,
        warmup_text: str = "Проверка связи",
        clock: object = time.monotonic,
    ) -> None:
        """Запомнить оба бэкенда. Загрузка -- в load()."""
        self._primary = primary
        self._fallback = fallback
        self.sample_rate = primary.sample_rate
        self._retry_s = retry_s
        self._persistent_retry_s = persistent_retry_s
        self._block = max(1, int(self.sample_rate * block_ms / 1000))
        self._warmup_text = warmup_text
        self._clock = clock
        self._primary_loaded = False
        self._retry_at = 0.0
        self.degraded_reason = ""
        """Пусто -- говорит основной; иначе причина перехода на запасной (для /diagnostics)."""

    def load(self) -> None:
        """Загрузить и разогреть запасной (обязан), затем основной (может не выйти).

        Разогрев запасного -- здесь: tts_node разогревает только тот путь,
        по которому пойдёт warmup_text, а это основной; холодный Silero
        отдал бы первую же подменённую реплику с лишней задержкой.
        """
        self._fallback.load()
        for _ in self._fallback.synthesize(self._warmup_text):
            pass
        try:
            self._primary.load()
        except Exception as error:
            self.degraded_reason = f"основной бэкенд не загрузился: {error}"
            _logger.error("%s -- говорю запасным до перезапуска", self.degraded_reason)
            return
        self._primary_loaded = True

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        """Основной, если он не на паузе; иначе или при его отказе -- запасной."""
        if self._primary_loaded and self._clock() >= self._retry_at:  # type: ignore[operator]
            yielded = False
            try:
                for block in self._primary.synthesize(text, voice):
                    yielded = True
                    yield block
            except Exception as error:
                persistent = isinstance(error, TtsServiceError) and error.persistent
                delay = self._persistent_retry_s if persistent else self._retry_s
                self._retry_at = self._clock() + delay  # type: ignore[operator]
                self.degraded_reason = f"{error} (повтор через {delay:.0f} с)"
                _logger.warning("основной TTS отказал, перехожу на запасной: %s", error)
                if yielded:
                    raise
            else:
                if self.degraded_reason:
                    _logger.info("основной TTS снова работает")
                    self.degraded_reason = ""
                return
        yield from self._fallback_blocks(text)

    def _fallback_blocks(self, text: str) -> Iterator[np.ndarray]:
        """Запасной синтез, пересчитанный к частоте основного."""
        blocks = list(self._fallback.synthesize(text, ""))
        if not blocks:
            return
        pcm = resample_int16(np.concatenate(blocks), self._fallback.sample_rate, self.sample_rate)
        for offset in range(0, pcm.size, self._block):
            yield pcm[offset : offset + self._block]

    def close(self) -> None:
        """Отпустить оба бэкенда."""
        self._primary.close()
        self._fallback.close()


def make_backend(kind: str, **kwargs: object) -> TtsBackend:
    """Собрать бэкенд по имени из параметров ноды."""
    factories = {
        "null": NullBackend,
        "piper": PiperBackend,
        "silero": SileroBackend,
        "yandex": YandexBackend,
    }
    if kind not in factories:
        raise ValueError(
            f"неизвестный бэкенд TTS: {kind!r}, ожидается один из {sorted(factories)}"
        )
    return factories[kind](**kwargs)  # type: ignore[arg-type,return-value]
