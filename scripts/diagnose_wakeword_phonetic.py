#!/usr/bin/env python3
"""Read-only diagnostics for the small head's auxiliary character predictions.

Word endpoints come from external ASR hints, so this is NOT a streaming KWS test.
It helps determine whether the auxiliary branch learned phonetic distinctions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

try:
    from .compare_wakeword_session import timing_hints, words_with_times
    from .train_wakeword_trial import DATA, read_wav
    from .wakeword_mel import feature_window, spectrogram
    from .wakeword_phonetic import build_model, decode_characters
except ImportError:
    from compare_wakeword_session import timing_hints, words_with_times
    from train_wakeword_trial import DATA, read_wav
    from wakeword_mel import feature_window, spectrogram
    from wakeword_phonetic import build_model, decode_characters


def main():
    """Проверить фонетическую модель на сохранённых тестовых записях."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(DATA / "experiments/_python_deps"))
    import torch
    from threadpoolctl import threadpool_limits

    torch.set_num_threads(2)
    _limiter = threadpool_limits(limits=1, user_api="blas")
    state = torch.load(args.experiment / "firaya_trial.pt", map_location="cpu", weights_only=True)
    stride = (
        json.loads((args.experiment / "results.json").read_text())
        .get("parameters", {})
        .get("time_stride", 4)
    )
    model = build_model(state["mean"].numpy(), state["std"].numpy(), stride)
    model.load_state_dict(state)
    model.eval()
    analysis = json.loads((args.analysis / "analysis.json").read_text())
    if Path(analysis["session"]).resolve() != args.session.resolve():
        raise ValueError("analysis belongs to another session")
    results = []
    with torch.no_grad():
        for record in analysis["records"]:
            wav = args.session / record["record"]["file"]
            if hashlib.sha256(wav.read_bytes()).hexdigest() != record["wav_sha256"]:
                raise ValueError("live recording changed")
            mel = spectrogram(np.concatenate([np.zeros(32000, np.int16), read_wav(wav)]))
            targets = [
                word
                for hint in timing_hints(record["record"], record["asr"])
                for word in hint["candidates"]
            ]
            for word in words_with_times(record["asr"]):
                end = min(len(mel), round((word["end"] + 2.16) * 100))
                window = feature_window(mel, end)
                prediction = decode_characters(
                    model.ctc(torch.from_numpy(window[None])).numpy()[0]
                )
                expected = any(abs(target["start"] - word["start"]) < 0.01 for target in targets)
                results.append(
                    {
                        "key": record["key"],
                        "word_hint": word,
                        "expected_name_hint": expected,
                        "ctc_prediction": prediction,
                    }
                )
                if expected:
                    print(record["key"], word["start"], word["word"], "=>", prediction, flush=True)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "diagnostics.json").write_text(
        json.dumps(
            {
                "experiment": str(args.experiment),
                "session": str(args.session),
                "purpose": "diagnostic only, external ASR endpoints; not streaming detection",
                "label_verified": False,
                "rows": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
