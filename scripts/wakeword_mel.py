"""Small language-independent log-mel frontend and temporal keyword classifier.

Unlike the embedding prototype this preserves 80 spectral bands for the head.
All preprocessing is deterministic, right-aligned and causal; no ASR at runtime.
"""

from __future__ import annotations

import numpy as np

RATE = 16000
HOP = 160
FFT = 512
WINDOW = 400
CONTEXT = 160
BANDS = 80
FRONTEND_VERSION = 1


def filterbank():
    """Построить треугольные mel-фильтры для фиксированного frontend."""
    points = np.linspace(0, 2595 * np.log10(1 + (RATE / 2) / 700), BANDS + 2)
    frequencies = 700 * (10 ** (points / 2595) - 1)
    bins = np.arange(FFT // 2 + 1) * RATE / FFT
    left = (bins[None, :] - frequencies[:-2, None]) / np.diff(frequencies)[:-1, None]
    right = (frequencies[2:, None] - bins[None, :]) / np.diff(frequencies)[1:, None]
    return np.maximum(0, np.minimum(left, right)).astype(np.float32)


FILTERS = filterbank()
HAMMING = np.hamming(WINDOW).astype(np.float32)


def _transform(frames):
    spectrum = np.fft.rfft(frames * HAMMING, n=FFT, axis=-1)
    power = (spectrum.real**2 + spectrum.imag**2).astype(np.float32)
    return np.log(np.maximum(power @ FILTERS.T, 1e-8)).astype(np.float32)


def spectrogram(audio):
    """Frame k ends at (k+1)*10ms; zero-pad only before the initial waveform."""
    audio = np.asarray(audio, dtype=np.float32) / 32768
    audio = np.pad(audio, (WINDOW - HOP, (-len(audio)) % HOP))
    frames = np.lib.stride_tricks.sliding_window_view(audio, WINDOW)[::HOP]
    return _transform(frames)


def feature_window(mel, end_frame):
    """Return exactly 1.6s of past features ending at an 80ms audio-clock boundary."""
    values = mel[max(0, end_frame - CONTEXT) : end_frame]
    if len(values) < CONTEXT:
        silence = spectrogram(np.zeros(HOP, dtype=np.int16))[0]
        values = np.concatenate([np.tile(silence, (CONTEXT - len(values), 1)), values])
    return values.astype(np.float32, copy=False)


def build_model(mean, std):
    """Собрать обучаемую голову для подготовленных акустических признаков."""
    import torch
    from torch import nn

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer("mean", torch.from_numpy(np.asarray(mean, np.float32)))
            self.register_buffer("std", torch.from_numpy(np.asarray(std, np.float32)))
            self.front = nn.Sequential(
                nn.Conv2d(1, 8, (5, 5), padding=2, stride=(1, 2)),
                nn.BatchNorm2d(8),
                nn.ReLU(),
                nn.Conv2d(8, 16, (5, 5), padding=2, stride=(2, 2)),
                nn.BatchNorm2d(16),
                nn.ReLU(),
                nn.Conv2d(16, 32, (5, 3), padding=(2, 1), stride=(2, 2)),
                nn.BatchNorm2d(32),
                nn.ReLU(),
            )
            self.temporal = nn.Sequential(
                nn.Conv1d(32, 48, 5, padding=4, dilation=2),
                nn.ReLU(),
                nn.Conv1d(48, 48, 5, padding=6, dilation=3),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.AdaptiveMaxPool1d(1),
                nn.Flatten(),
                nn.Linear(48, 1),
            )

        def forward(self, value):
            value = (value - self.mean) / self.std
            # Conv2D axes: time and frequency. Pool only the frequency axis first.
            value = self.front(value[:, None]).mean(dim=-1)
            return self.temporal(value)

    return Head()


class MelRuntime:
    """The same predict/reset surface used by the evaluation runner; CPU only."""

    def __init__(self, model_path, config):
        """Загрузить локальную ONNX-модель и проверить версию frontend."""
        import onnxruntime as ort

        if config.get("frontend_version") != FRONTEND_VERSION:
            raise ValueError("unsupported log-mel frontend version")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self):
        """Сбросить историю акустических окон перед новым потоком."""
        self.buffer = np.zeros(WINDOW + (CONTEXT - 1) * HOP, dtype=np.float32)

    def predict(self, chunk):
        """Вычислить прогноз на одном кадре mono PCM16 длительностью 80 мс."""
        from threadpoolctl import threadpool_limits

        if len(chunk) != 1280:
            raise ValueError("expected one 80ms mono PCM16 frame")
        self.buffer = np.concatenate([self.buffer[len(chunk) :], chunk.astype(np.float32) / 32768])
        frames = np.lib.stride_tricks.sliding_window_view(self.buffer, WINDOW)[::HOP]
        with threadpool_limits(limits=1, user_api="blas"):
            value = _transform(frames)
        score = self.session.run(None, {"audio_features": value[None]})[0].reshape(-1)[0]
        return {"firaya_trial": float(score)}
