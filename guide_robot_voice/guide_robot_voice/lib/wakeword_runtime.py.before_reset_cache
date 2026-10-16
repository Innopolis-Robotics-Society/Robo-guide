"""Локальный frozen openWakeWord runtime и sample-indexed PCM framing.

Никаких загрузок из сети, ASR или зависимостей от обучающих scripts/.
Все три ONNX и параметры проверяются по SHA256 перед созданием runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

RATE = 16_000
FRAME = 1_280
FILES = ("firaya_trial.onnx", "melspectrogram.onnx", "embedding_model.onnx")
FEATURE_FRAMES = 16
FEATURE_DIM = 96
MAX_CONFIRMATION_FRAMES = 20


class _WakewordScorer(Protocol):
    """Минимальный интерфейс модели для CPU-потока и тестовых реализаций."""

    def reset(self) -> None:
        """Сбросить историю модели перед новой capture-сессией."""
        ...

    def score(self, pcm: np.ndarray) -> float:
        """Вернуть score для одного полного кадра PCM16."""
        ...


def validate_bundle(directory: Path) -> dict:
    """Проверить артефакты и зафиксированные параметры без загрузки модели."""
    manifest = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    config = manifest["inference_config"]
    if manifest.get("runtime") != "openwakeword==0.6.0":
        raise ValueError("Нужен frozen runtime openwakeword==0.6.0")
    if config.get("model") != FILES[0] or config.get("feature_kind") is not None:
        raise ValueError("Ожидается embedding-модель firaya_trial.onnx")
    if config.get("feature_frames") != FEATURE_FRAMES or config.get("feature_dim") != FEATURE_DIM:
        raise ValueError("Ожидаются 16 x 96 embedding features")
    threshold = config.get("threshold")
    cooldown = config.get("cooldown_seconds")
    confirmation = config.get("consecutive_frames")
    if not isinstance(threshold, int | float) or not 0 < threshold <= 1:
        raise ValueError("Порог должен быть в (0, 1]")
    if type(confirmation) is not int or not 1 <= confirmation <= MAX_CONFIRMATION_FRAMES:
        raise ValueError("Подтверждение: 1..20 соседних кадров")
    if not isinstance(cooldown, int | float) or not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("Некорректный cooldown")
    for name in FILES:
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if digest != manifest["sha256"].get(name):
            raise ValueError(f"SHA256 mismatch: {name}")
    return manifest


class FrozenWakewordModel:
    """Тот же CPU ONNX inference и прогрев, что в прослушанных live-тестах."""

    def __init__(self, directory: Path) -> None:
        """Загрузить только локальные явно указанные ONNX."""
        from importlib.metadata import version

        self.manifest = validate_bundle(directory)
        if version("openwakeword") != "0.6.0":
            raise ValueError("Установите openwakeword==0.6.0")
        from openwakeword.model import Model

        self.model = Model(
            wakeword_models=[str(directory / FILES[0])],
            inference_framework="onnx",
            melspec_model_path=str(directory / FILES[1]),
            embedding_model_path=str(directory / FILES[2]),
            ncpu=1,
        )

    def reset(self) -> None:
        """Сбросить историю; добавить 2 с нулей ДО потока, не после."""
        self.model.reset()
        for _ in range(25):
            self.model.predict(np.zeros(FRAME, dtype=np.int16))

    def score(self, pcm: np.ndarray) -> float:
        """Один score на реальные 80 мс моно PCM16."""
        return float(self.model.predict(pcm)["firaya_trial"])


@dataclass(frozen=True)
class Detection:
    """Привязка подтверждённого кандидата к концу capture-окна."""

    device_session_id: str
    end_sample: int
    timestamp: float
    confidence: float


class WakewordStream:
    """Собирать любые соседние chunks в 1280 samples без дублирования/паддинга."""

    def __init__(self, model: _WakewordScorer, config: dict) -> None:
        """Модель предоставляет reset()/score(); конфигурация уже проверена."""
        self.model = model
        self.threshold = float(config["threshold"])
        self.confirmation = int(config["consecutive_frames"])
        self.cooldown_samples = round(float(config["cooldown_seconds"]) * RATE)
        self.reset()

    def reset(self) -> None:
        """Забыть PCM и подтверждение на lifecycle reset или reconnect."""
        self.session = ""
        self.next_sample = None
        self.pending = np.zeros(0, dtype=np.int16)
        self.streak = 0
        self.last_trigger_sample = -(10**18)
        self.last_score = 0.0
        self.discontinuities = getattr(self, "discontinuities", 0)
        self.model.reset()

    def push(self, session: str, first: int, timestamp: float, pcm: np.ndarray) -> list[Detection]:
        """На разрыве сбросить весь контекст, не склеивать чужие сэмплы."""
        if not session or first < 0 or pcm.ndim != 1 or pcm.dtype != np.int16:
            raise ValueError("Нужен адресованный mono PCM16")
        if not pcm.size:
            return []
        if session != self.session or (self.next_sample is not None and first != self.next_sample):
            if self.session:
                self.discontinuities += 1
            self.reset()
            self.session = session
        pending_first = first - self.pending.size
        data = np.concatenate((self.pending, pcm))
        events = []
        consumed = 0
        while consumed + FRAME <= data.size:
            end = pending_first + consumed + FRAME
            score = self.model.score(data[consumed : consumed + FRAME])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("Некорректный KWS score")
            self.last_score = score
            self.streak = self.streak + 1 if score >= self.threshold else 0
            if (
                self.streak >= self.confirmation
                and end - self.last_trigger_sample >= self.cooldown_samples
            ):
                events.append(Detection(session, end, timestamp + (end - first) / RATE, score))
                self.last_trigger_sample = end
            consumed += FRAME
        self.pending = data[consumed:].copy()
        self.next_sample = first + pcm.size
        return events
