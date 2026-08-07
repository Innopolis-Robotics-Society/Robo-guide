"""GigaAM v3 CTC через sherpa-onnx.

Импорт sherpa_onnx отложен внутрь load(): та же причина, что и для
PiperBackend/SileroVad -- модуль обязан импортироваться в CI без модели
и без sherpa-onnx.

ПОЧЕМУ OfflineRecognizer, А НЕ OnlineRecognizer. Design (§3.4) закладывал
честный потоковый инференс через sherpa_onnx.OnlineRecognizer.from_nemo_ctc.
На практике экспорт GigaAM v3 CTC для sherpa-onnx (Smirnov75/GigaAM-v3-
sherpa-onnx на HuggingFace -- ближайшее публичное соответствие пути
model_dir из дизайна) не несёт метаданных cache-aware streaming: граф
падает на инициализации с "window_size does not exist in the metadata".
GigaAM как архитектура не экспортировался с причинным (causal) конформером
для потокового инференса -- честного стриминга для него в публичных
сборках по факту не существует, вопреки исходному предположению design.

Практический выход -- OfflineRecognizer, гоняемый повторно (см. asr_node):
партиалы считаются на ограниченном скользящем окне последних секунд
буфера (иначе время декодирования растёт с длиной высказывания и на
потолке max_utterance_s гарантированно вылетает за partial_rate_hz),
финал -- на полном высказывании целиком, спешить там некуда.

confidence: greedy_search у sherpa-onnx для nemo_ctc не заполняет
ys_log_probs (пусто на практике, проверено на реальной модели) -- в этом
случае возвращается -1.0, ровно как документирует Transcript.msg
("-1.0 если бэкенд не отдаёт"), а не выдумывается число.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["AsrResult", "GigaAmCtc"]


@dataclass(frozen=True)
class AsrResult:
    """Результат одного прохода распознавания."""

    text: str
    confidence: float
    """[0..1], среднее по токенам. -1.0, если бэкенд не отдаёт log-вероятности."""


class GigaAmCtc:
    """Offline-инференс GigaAM v3 CTC, вызываемый повторно для партиалов и финала."""

    def __init__(
        self,
        model_path: str,
        tokens_path: str,
        sample_rate: int = 16000,
        feature_dim: int = 80,
        num_threads: int = 2,
    ) -> None:
        """Запомнить пути и параметры. Модель загружается в load()."""
        self._model_path = model_path
        self._tokens_path = tokens_path
        self._sample_rate = sample_rate
        self._feature_dim = feature_dim
        self._num_threads = num_threads
        self._recognizer: object | None = None

    def load(self) -> None:
        """Загрузить ONNX-граф. Может быть медленным, звать в on_configure."""
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            tokens=self._tokens_path,
            model=self._model_path,
            num_threads=self._num_threads,
            sample_rate=self._sample_rate,
            feature_dim=self._feature_dim,
            decoding_method="greedy_search",
            provider="cpu",
        )

    def decode(self, pcm: np.ndarray) -> AsrResult:
        """Распознать моно int16 @ sample_rate одним проходом целиком."""
        if self._recognizer is None:
            raise RuntimeError("GigaAmCtc.load() не вызван")
        stream = self._recognizer.create_stream()
        audio = pcm.astype(np.float32) / 32768.0
        stream.accept_waveform(self._sample_rate, audio)
        self._recognizer.decode_stream(stream)
        result = stream.result

        log_probs = result.ys_log_probs
        confidence = min(max(float(np.exp(np.mean(log_probs))), 0.0), 1.0) if log_probs else -1.0
        return AsrResult(text=result.text, confidence=confidence)

    def close(self) -> None:
        """Освободить сессию."""
        self._recognizer = None
