#!/usr/bin/env python3
"""Offline diagnostics for saved live tests; never train on or alter recordings.

ASR text and VAD segments are hints, not verified word boundaries. Keep the
original cues alongside them: speakers may not follow the cue schedule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

try:
    from .train_wakeword_trial import DATA, PREFIX, RATE, read_wav, stream_features
except ImportError:
    from train_wakeword_trial import DATA, PREFIX, RATE, read_wav, stream_features


def bounded_decode(analyser, audio, core_seconds=16, context_seconds=1):
    """Keep ASR memory bounded on multi-minute tests; stitch whole timed words.

    Overlapping context is decoded but each word belongs to exactly one core by
    its start time. Character timestamps stay absolute, not collapsed to word onset.
    This is an offline diagnostic, not part of live KWS or a verified transcript.
    """
    if len(audio) <= 30 * RATE:
        return analyser.decode(audio)
    if core_seconds <= 0 or context_seconds < 0:
        raise ValueError("invalid ASR chunk parameters")
    tokens, timestamps = [], []
    size = round(core_seconds * RATE)
    overlap = round(context_seconds * RATE)
    for first in range(0, len(audio), size):
        last = min(len(audio), first + size)
        left, right = max(0, first - overlap), min(len(audio), last + overlap)
        decoded = analyser.decode(audio[left:right])
        text, times = "", []
        for token, time in zip(decoded["tokens"], decoded["timestamps"], strict=False):
            value = token.replace("▁", " ")
            text += value
            times.extend([time + left / RATE] * len(value))
        for match in re.finditer(r"[а-яё]+", text.lower()):
            onset = times[match.start()]
            if first / RATE <= onset < last / RATE:
                if tokens:
                    tokens.append(" ")
                    timestamps.append(onset)
                tokens.extend(text[match.start() : match.end()])
                timestamps.extend(times[match.start() : match.end()])
    try:
        from .prepare_wakeword_dataset import keyword_hints
    except ImportError:
        from prepare_wakeword_dataset import keyword_hints
    return {
        "text": "".join(tokens),
        "tokens": tokens,
        "timestamps": timestamps,
        "hints": keyword_hints(tokens, timestamps),
        "chunked": True,
        "core_seconds": core_seconds,
        "context_seconds": context_seconds,
    }


def main():
    """Проанализировать сохранённые тесты без изменения WAV или обучения."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    from openwakeword.utils import AudioFeatures
    from prepare_wakeword_dataset import Analyser

    args.output.mkdir(parents=True, exist_ok=False)
    extractor = AudioFeatures(
        inference_framework="onnx",
        ncpu=1,
        melspec_model_path=str(DATA / "experiments/_backbone/melspectrogram.onnx"),
        embedding_model_path=str(DATA / "experiments/_backbone/embedding_model.onnx"),
    )
    analyser = Analyser()
    records = []
    for path in sorted((args.session / "takes").glob("*.json")):
        if path.stem.endswith("_scores"):
            continue
        record = json.loads(path.read_text())
        if record["status"] != "complete":
            continue
        wav = args.session / record["file"]
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        if digest != record["wav_sha256"]:
            raise ValueError(f"Recording changed: {wav}")
        audio = read_wav(wav)
        value = stream_features(
            extractor,
            np.concatenate([np.zeros(int(PREFIX * RATE), dtype=np.int16), audio]),
            PREFIX,
        )
        np.save(args.output / f"{record['key']}.npy", value)
        regions = analyser.speech(audio)
        decoded = bounded_decode(analyser, audio)
        segments = []
        for start, end in regions:
            hint = bounded_decode(
                analyser,
                audio[
                    max(0, int((start - 0.15) * RATE)) : min(len(audio), int((end + 0.15) * RATE))
                ],
            )
            segments.append({"start": start, "end": end, "text_hint": hint["text"]})
        row = {
            "key": record["key"],
            "record": record,
            "wav_sha256": digest,
            "asr": decoded,
            "speech": segments,
            "label_verified": False,
        }
        records.append(row)
        print(record["key"], json.dumps(segments, ensure_ascii=False), flush=True)
    (args.output / "analysis.json").write_text(
        json.dumps(
            {
                "session": str(args.session),
                "backbone_sha256": {
                    name: hashlib.sha256(
                        (DATA / "experiments/_backbone" / name).read_bytes()
                    ).hexdigest()
                    for name in ("melspectrogram.onnx", "embedding_model.onnx")
                },
                "purpose": "evaluation only; ASR/VAD are unverified timing hints",
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
