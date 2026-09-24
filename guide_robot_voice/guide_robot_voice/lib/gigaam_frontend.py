"""Препроцессинг и CTC-декодирование GigaAM v3 без sherpa-onnx.

sherpa-onnx в образе собран без GPU, а onnxruntime-gpu в нём есть: граф
GigaAM принимает готовые log-mel признаки, так что достаточно посчитать их
самим и отдать в onnxruntime с CUDA.

Признаки -- как в официальном `gigaam.preprocess.FeatureExtractor` для v3
(`v3_ctc.yaml`): torchaudio MelSpectrogram с n_fft=win=320, hop=160,
center=False, 64 мела по шкале HTK без нормировки, окно Ханна
(периодическое), power=2, затем log(clamp(x, 1e-9, 1e9)). sherpa-onnx
считает их по-своему (kaldi-fbank), поэтому тексты изредка расходятся на
словах вне словаря вроде «Фирая».

Модуль без rclpy и без onnxruntime: тестируется в CI на numpy.
"""

from __future__ import annotations

from functools import cache

import numpy as np

__all__ = ["SAMPLE_RATE", "ctc_greedy", "load_tokens", "log_mel"]

SAMPLE_RATE = 16000
_N_FFT = 320
_HOP = 160
_N_MELS = 64


@cache
def _mel_filterbank() -> np.ndarray:
    """Треугольные фильтры HTK, как torchaudio.functional.melscale_fbanks(norm=None)."""
    n_freqs = _N_FFT // 2 + 1
    all_freqs = np.linspace(0.0, SAMPLE_RATE // 2, n_freqs)
    m_max = 2595.0 * np.log10(1.0 + (SAMPLE_RATE / 2) / 700.0)
    m_pts = np.linspace(0.0, m_max, _N_MELS + 2)
    f_pts = 700.0 * (10.0 ** (m_pts / 2595.0) - 1.0)
    f_diff = f_pts[1:] - f_pts[:-1]
    slopes = f_pts[None, :] - all_freqs[:, None]
    down = -slopes[:, :-2] / f_diff[:-1]
    up = slopes[:, 2:] / f_diff[1:]
    return np.maximum(0.0, np.minimum(down, up)).astype(np.float32)


@cache
def _window() -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(_N_FFT) / _N_FFT)).astype(np.float32)


def log_mel(pcm: np.ndarray) -> np.ndarray:
    """int16 моно 16 кГц -> признаки (64, T) float32. Короче одного окна -- T=0."""
    audio = pcm.astype(np.float32) / 32768.0
    if audio.shape[0] < _N_FFT:
        return np.zeros((_N_MELS, 0), dtype=np.float32)
    frames = 1 + (audio.shape[0] - _N_FFT) // _HOP
    index = np.arange(_N_FFT)[None, :] + _HOP * np.arange(frames)[:, None]
    power = np.abs(np.fft.rfft(audio[index] * _window(), n=_N_FFT)) ** 2
    mel = power.astype(np.float32) @ _mel_filterbank()
    return np.log(np.clip(mel, 1e-9, 1e9)).T.astype(np.float32)


def load_tokens(path: str) -> list[str]:
    """tokens.txt sherpa-формата («<символ> <id>», пробел -- пустой символ) -> список по id."""
    table: dict[int, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            symbol, _, index = line.rpartition(" ")
            table[int(index)] = symbol if symbol else " "
    return [table[i] for i in range(len(table))]


def ctc_greedy(log_probs: np.ndarray, vocab: list[str], blank: int) -> tuple[str, float]:
    """(T, V) log-вероятности -> (текст, уверенность [0..1] по выданным токенам).

    Уверенность -- exp(среднего max log p) по кадрам, давшим символ; -1.0,
    если символов нет (как в Transcript.msg).
    """
    if log_probs.shape[0] == 0:
        return "", -1.0
    ids = log_probs.argmax(axis=-1)
    best = log_probs.max(axis=-1)
    chars: list[str] = []
    scores: list[float] = []
    previous = -1
    for frame, raw in enumerate(ids):
        token = int(raw)
        if token not in (previous, blank) and token < len(vocab):
            chars.append(vocab[token])
            scores.append(float(best[frame]))
        previous = token
    text = " ".join("".join(chars).split())
    confidence = min(max(float(np.exp(np.mean(scores))), 0.0), 1.0) if scores else -1.0
    return text, confidence
