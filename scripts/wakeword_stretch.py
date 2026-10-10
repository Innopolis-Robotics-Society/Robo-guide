"""Pitch-preserving duration augmentation; a phase-vocoder approximation.

This is training data augmentation, not enhancement of the microphone signal.
Transients may smear; the same transform is applied to both label classes.
"""

from __future__ import annotations

import numpy as np


def time_stretch(audio, rate):
    """Изменить темп mono PCM16, сохраняя длину согласно заданной скорости."""
    from scipy.signal import istft, stft

    audio = np.asarray(audio)
    if audio.ndim != 1 or len(audio) < 512 or audio.dtype != np.int16:
        raise ValueError("expected at least 512 mono PCM16 samples")
    if not np.isfinite(rate) or not 0.5 <= rate <= 2:
        raise ValueError("rate must be finite in [.5, 2]")
    if rate == 1:
        return audio.copy()
    fft, hop = 512, 128
    _, _, spectrum = stft(
        audio.astype(np.float64),
        window="hann",
        nperseg=fft,
        noverlap=fft - hop,
        boundary="zeros",
        padded=True,
    )
    steps = np.arange(0, spectrum.shape[1] - 1, rate)
    phase = np.angle(spectrum[:, 0])
    advance = np.arange(spectrum.shape[0]) * 2 * np.pi * hop / fft
    stretched = np.empty((spectrum.shape[0], len(steps)), dtype=np.complex128)
    for index, step in enumerate(steps):
        first = int(step)
        fraction = step - first
        pair = spectrum[:, first : first + 2]
        magnitude = (1 - fraction) * np.abs(pair[:, 0]) + fraction * np.abs(pair[:, 1])
        stretched[:, index] = magnitude * np.exp(1j * phase)
        delta = np.angle(pair[:, 1]) - np.angle(pair[:, 0]) - advance
        delta -= 2 * np.pi * np.round(delta / (2 * np.pi))
        phase += advance + delta
    _, values = istft(stretched, window="hann", nperseg=fft, noverlap=fft - hop, boundary=True)
    target_length = round(len(audio) / rate)
    values = np.pad(values, (0, max(0, target_length - len(values))))[:target_length]
    return np.clip(np.rint(values), -32768, 32767).astype(np.int16)
