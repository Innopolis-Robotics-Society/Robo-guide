"""A small spectral/temporal KWS head with training-only character supervision.

The frequency axis is flattened, not averaged: absolute formant locations remain
available. CTC is an auxiliary training objective, not an ASR dependency at runtime.
All inference inputs are past audio; bidirectionality is within that past window.
"""

from __future__ import annotations

import re

import numpy as np

ALPHABET = "_ абвгдежзийклмнопрстуфхцчшщъыьэюя"


def normalize_text(text):
    """Нормализовать русские буквы и пробелы для CTC-разметки."""
    return " ".join(re.findall(r"[а-я]+", text.lower().replace("ё", "е")))


def encode_text(text):
    """Преобразовать нормализованный текст в индексы CTC-алфавита."""
    text = normalize_text(text)
    if not text:
        raise ValueError("empty character target")
    return np.asarray([ALPHABET.index(char) for char in text], dtype=np.int64)


def decode_characters(logprob):
    """Greedy CTC diagnostic: collapse repeats before removing blank symbols."""
    indices = np.asarray(logprob).argmax(-1)
    previous, result = None, []
    for index in indices:
        if index != previous and index != 0:
            result.append(ALPHABET[int(index)])
        previous = index
    return "".join(result).strip()


def reviewed_interval(row):
    """Recover approximate speech bounds from the actual crop method.

    Human acceptance proves inclusion of the name, not precise word timestamps.
    Unlocalized full recordings must not become time-local positive supervision.
    """
    method = row["method"]
    if method == "full_recording":
        return None
    if method == "single_vad_region" and len(row["vad_regions"]) == 1:
        return tuple(row["vad_regions"][0])
    padding = 0.35 if method == "asr_hint" else 0.2
    start = row["crop_start_seconds"]
    end = row["crop_end_seconds"]
    start = start + padding if start > 0 else 0
    end = end - padding if end < row["source_seconds"] else end
    return (start, end) if end > start else None


def consecutive_groups(positions):
    """Never count nonadjacent negative frames as a confirmation streak."""
    positions = np.asarray(positions)
    if not len(positions):
        return []
    return np.split(positions, np.flatnonzero(np.diff(positions) != 8) + 1)


def word_positions(mel_frames, interval=None, complete_word=False):
    """Выбрать кадры имени с учётом режима полного слова."""
    ends = np.arange(8, mel_frames + 1, 8)
    times = ends / 100 - 2
    if interval is None:
        return np.zeros(0, int), ends[times > 0]
    start, end = interval
    if complete_word:
        # Delay positives past approximate word ending. The first half of the
        # name is explicitly not yet a wake event; uncertain boundaries are ignored.
        positive = ends[(times >= end + 0.20) & (times <= end + 0.64)]
        before = start + 0.5 * (end - start)
    else:
        positive = ends[(times >= end + 0.08) & (times <= end + 0.56)]
        before = start
    negative = ends[(times > 0) & ((times < before) | (times > end + 1.6))]
    return positive, negative


def build_model(mean, std, time_stride=4):
    """Собрать обучаемую голову для подготовленных акустических признаков."""
    import torch
    from torch import nn

    if time_stride not in (2, 4):
        raise ValueError("time_stride must be 2 or 4")

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer("mean", torch.from_numpy(np.asarray(mean, np.float32)))
            self.register_buffer("std", torch.from_numpy(np.asarray(std, np.float32)))
            self.front = nn.Sequential(
                nn.Conv2d(1, 8, 5, padding=2, stride=(1, 2)),
                nn.BatchNorm2d(8),
                nn.ReLU(),
                nn.Conv2d(8, 16, 5, padding=2, stride=(2, 2)),
                nn.BatchNorm2d(16),
                nn.ReLU(),
                nn.Conv2d(16, 32, (5, 3), padding=(2, 1), stride=(time_stride // 2, 2)),
                nn.BatchNorm2d(32),
                nn.ReLU(),
            )
            self.project = nn.Sequential(nn.Linear(320, 96), nn.LayerNorm(96), nn.ReLU())
            self.gru = nn.GRU(96, 64, batch_first=True, bidirectional=True)
            self.characters = nn.Linear(128, len(ALPHABET))
            self.classifier = nn.Sequential(
                nn.Linear(384, 64), nn.LayerNorm(64), nn.ReLU(), nn.Dropout(0.1), nn.Linear(64, 1)
            )

        def sequence(self, value):
            values = self.front(((value - self.mean) / self.std)[:, None])
            values = values.permute(0, 2, 1, 3).flatten(2)
            return self.gru(self.project(values))[0]

        def ctc(self, value):
            return self.characters(self.sequence(value)).log_softmax(-1)

        def forward(self, value):
            sequence = self.sequence(value)
            pooled = torch.cat([sequence.amax(1), sequence.mean(1), sequence[:, -1]], dim=1)
            return self.classifier(pooled)

    return Head()
