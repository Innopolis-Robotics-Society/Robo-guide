"""Silero VAD v5 через onnxruntime.

Импорт onnxruntime отложен внутрь load(): модуль обязан импортироваться
в CI, где ни модели, ни onnxruntime может не быть -- та же причина,
что и для PiperBackend в lib/backends.py.

Контракт входа/выхода ONNX-графа: `input` [batch, samples] float32,
`state` [2, batch, 128] float32, `sr` () int64 -> `output` [batch, 1]
float32 (вероятность речи), `stateN` -- обновлённое состояние RNN.
Форма `input` в графе динамическая ([None, None]), из-за чего легко
ошибиться: граф не откажет, если скормить ровно 512 сэмплов без контекста,
но вероятность на реальной речи при этом останется околонулевой -- граф
рассчитан на 512 НОВЫХ сэмплов, дополненных СЛЕВА 64 сэмплами хвоста
предыдущего окна (context, 4 мс на 16 кГц), итого 576 на вход. Без этого
свёрточные слои в начале графа получают контекст из нулей на каждом окне,
и модель эффективно слышит только первые ~448 сэмплов из 512 -- это не
задокументировано в самой ONNX-модели никак, взято из референсного
Python-обёртчика (github.com/snakers4/silero-vad, utils_vad.py,
OnnxWrapper.__call__): `x = torch.cat([self._context, x]); ...;
self._context = x[..., -context_size:]`. Контекст переносится между
окнами так же, как state.

Окно обязано быть ровно 512 сэмплов на 16 кГц -- design §3.1 объясняет,
почему кадр audio_frontend подобран так, чтобы 512 = 2 кадра захвата.
Вход нормализуется в [-1, 1]: модель обучена на аудио в этом диапазоне,
а не на сыром int16.
"""

from __future__ import annotations

import numpy as np

__all__ = ["SileroVad"]

_WINDOW_SAMPLES = 512
_CONTEXT_SAMPLES = 64
_STATE_SHAPE = (2, 1, 128)


class SileroVad:
    """Потоковый инференс Silero VAD v5. Состояние и контекст переносятся между окнами."""

    def __init__(self, model_path: str, sample_rate: int = 16000) -> None:
        """Запомнить путь и частоту. Модель загружается в load()."""
        self._model_path = model_path
        self._sample_rate = sample_rate
        self._session: object | None = None
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)

    def load(self) -> None:
        """Загрузить ONNX-граф. Может быть медленным, звать в on_configure."""
        import onnxruntime as ort

        self._session = ort.InferenceSession(self._model_path, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        """Сбросить состояние RNN и контекст. Звать при разрыве потока (xrun) и на activate()."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)

    def process(self, window: np.ndarray) -> float:
        """Вероятность речи для окна ровно 512 сэмплов int16."""
        if self._session is None:
            raise RuntimeError("SileroVad.load() не вызван")
        if window.shape[0] != _WINDOW_SAMPLES:
            raise ValueError(
                f"окно Silero VAD обязано быть {_WINDOW_SAMPLES} сэмплов, "
                f"получено {window.shape[0]}"
            )
        normalized = window.astype(np.float32) / 32768.0
        audio = np.concatenate([self._context, normalized]).reshape(1, -1)
        sr = np.array(self._sample_rate, dtype=np.int64)
        output, self._state = self._session.run(
            ["output", "stateN"], {"input": audio, "state": self._state, "sr": sr}
        )
        self._context = normalized[-_CONTEXT_SAMPLES:]
        return float(output[0, 0])

    def close(self) -> None:
        """Освободить сессию."""
        self._session = None
