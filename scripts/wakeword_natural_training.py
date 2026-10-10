"""Natural full-phrase TTS features and independent approximate name timing.

The live protocol is never opened. Timing comes from a separate frozen ASR and
the known positive synthesis text. Ambiguous alignments are excluded explicitly.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

try:
    from .compare_wakeword_session import words_with_times
    from .prepare_wakeword_dataset import Analyser, _distance
    from .train_wakeword_trial import RATE, read_wav
    from .wakeword_synthetic_training import add_synthetic, load_synthetic
except ImportError:
    from compare_wakeword_session import words_with_times
    from prepare_wakeword_dataset import Analyser, _distance
    from train_wakeword_trial import RATE, read_wav
    from wakeword_synthetic_training import add_synthetic, load_synthetic


def load_natural(root, output, raw, plan, backbone, reuse=None):
    """Подготовить признаки непрерывных фраз и подсказки времени имени."""
    synth_plan, rows = load_synthetic(root)
    if synth_plan["mode"] != "continuous":
        raise ValueError("expected naturally synthesized continuous phrases")
    output.mkdir()
    (output / "features").mkdir()
    if reuse:
        old = json.loads((reuse / "natural_plan.json").read_text())
        if (
            old["manifest_sha256"]
            != hashlib.sha256((root / "manifest.jsonl").read_bytes()).hexdigest()
        ):
            raise ValueError("natural dataset changed")
        timings = json.loads((reuse / "timings.json").read_text())
    else:
        analyser = Analyser()
        timings = {}
        for row in rows:
            sample = row["sample"]
            if sample["kind"] != "positive":
                continue
            audio = read_wav(root / sample["file"])
            stream = analyser.asr.create_stream()
            stream.accept_waveform(RATE, audio.astype(np.float32) / 32768)
            analyser.asr.decode_stream(stream)
            tokens = {
                "tokens": list(stream.result.tokens),
                "timestamps": list(stream.result.timestamps),
            }
            words = words_with_times(tokens)
            candidates = [
                word
                for word in words
                if word["word"].startswith(("ф", "сир", "вчер"))
                and min(_distance(word["word"], target) for target in ("фирая", "фирайя")) <= 3
            ]
            timings[sample["file"]] = {
                "candidates": candidates,
                "asr_text": stream.result.text,
                "source_sha256": row["sha256"],
                "label_verified": False,
            }
        print(
            f"Natural timing hints: "
            f"{sum(len(r['candidates']) == 1 for r in timings.values())}/{len(timings)} "
            f"unambiguous",
            flush=True,
        )
    temp = {split: {kind: [] for kind in ("positive", "negative")} for split in ("train", "val")}
    features = add_synthetic(temp, rows, root, raw, plan, output, backbone, 1337, reuse)
    (output / "split_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    (output / "feature_provenance.json").write_text(
        json.dumps(
            {"seed": 1337, "backbone": json.loads((backbone / "provenance.json").read_text())},
            indent=2,
        )
    )
    (output / "timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2))
    (output / "natural_plan.json").write_text(
        json.dumps(
            {
                "source_root": str(root),
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.jsonl").read_bytes()
                ).hexdigest(),
                "alignment": "frozen GigaAM approximate token times; exclude ambiguous positives",
            },
            indent=2,
        )
    )
    return features, timings
