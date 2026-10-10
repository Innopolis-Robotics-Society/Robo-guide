#!/usr/bin/env python3
"""Generate naturally synthesized continuous phrases with matched non-name text.

Local approved Silero voices and stress ФирАя, no network/API/key. Text families,
all voices and rates stay in a single train/aux-validation split. Does not use
the phrases or WAVs from the live evaluation protocol.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

try:
    from .generate_wakeword_silero import (
        LICENSE_URL,
        MODEL,
        SPEAKERS,
        SYNTHETIC,
        encode_json,
        generate,
        load_model,
    )
except ImportError:
    from generate_wakeword_silero import (
        LICENSE_URL,
        MODEL,
        SPEAKERS,
        SYNTHETIC,
        encode_json,
        generate,
        load_model,
    )

TEMPLATES = (
    ("begin", "{name} подскаж+и как пройт+и к л+естнице"),
    ("middle", "Подскаж+и {name} как пройт+и к л+естнице"),
    ("end", "Подскаж+и как пройт+и к л+естнице {name}"),
    ("middle", "Сейч+ас {name} я зад+ам друг+ой вопр+ос"),
    ("begin", "{name} мы немн+ого заблуд+ились и +ищем гардер+об"),
    ("middle", "Посл+ушай {name} мы немн+ого заблуд+ились"),
    ("end", "Мы немн+ого заблуд+ились и +ищем гардер+об {name}"),
    ("middle", "В конц+е з+ала {name} в+исит портр+ет"),
    ("begin", "{name} почем+у +этот экспон+ат сто+ит отд+ельно"),
    ("middle", "Объясн+и {name} почем+у экспон+ат сто+ит отд+ельно"),
    ("end", "Почем+у +этот экспон+ат сто+ит отд+ельно {name}"),
    ("middle", "Дав+айте {name} обс+удим распис+ание экск+урсии"),
    ("begin", "{name} мне н+ужно найти остальн+ых уч+астников гр+уппы"),
    ("middle", "Пож+алуйста {name} помог+и найт+и уч+астников гр+уппы"),
    ("end", "Мне н+ужно найти остальн+ых уч+астников гр+уппы {name}"),
    ("middle", "+Это {name} наш п+ервый виз+ит сюд+а"),
)
SUBSTITUTES = ("Фи+она", "Фар+ида", "Ф+ира", "Ф+урия", "Ферр+ари", "Р+ая", "Мар+ия", "Ф+ая")


def make_plan(rates=(90, 110)):
    """Составить план непрерывных положительных и отрицательных TTS-фраз."""
    if (
        not rates
        or len(set(rates)) != len(rates)
        or any(rate < 50 or rate > 200 for rate in rates)
    ):
        raise ValueError("rates must be unique percentages in [50, 200]")
    samples = []
    for index, (position, template) in enumerate(TEMPLATES):
        split = "val" if index % 4 == 3 else "train"
        for speaker in SPEAKERS:
            for rate in rates:
                for kind, name in (
                    ("positive", "Фир+ая"),
                    ("negative", SUBSTITUTES[index % len(SUBSTITUTES)]),
                ):
                    params = {
                        "speaker": speaker,
                        "text_with_stress": template.format(name=name),
                        "rate_percent": rate,
                        "tts_sample_rate": 24000,
                        "seed": 1337,
                        "model_id": "v5_cis_base",
                    }
                    identity = hashlib.sha256(encode_json(params)).hexdigest()
                    samples.append(
                        {
                            "id": identity,
                            "kind": kind,
                            "text": params["text_with_stress"].replace("+", ""),
                            "parameters": params,
                            "stress": "a" if kind == "positive" else None,
                            "file": f"{kind}/{speaker}/continuous_{index:02d}_{rate}.wav",
                            "training_split": split,
                            "target_position": position,
                            "text_family": index,
                        }
                    )
    return {
        "schema_version": 1,
        "provider": "silero",
        "model_id": "v5_cis_base",
        "license": "MIT",
        "license_url": LICENSE_URL,
        "mode": "continuous",
        "sample_rate": 16000,
        "sample_width": 2,
        "channels": 1,
        "split": "train_only",
        "review_required": True,
        "training_ready": False,
        "samples": samples,
    }


def main():
    """Сформировать или выполнить план непрерывного синтеза."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SYNTHETIC / "silero_continuous_01")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rates", type=int, nargs="+", default=[90, 110])
    args = parser.parse_args()
    plan = make_plan(args.rates)
    print(f"{len(plan['samples'])} WAV, естественно синтезированные фразы, 4 голоса", flush=True)
    if not args.execute:
        print(encode_json(plan).decode())
        return
    model, digest = load_model(MODEL, 4)
    generate(model, digest, plan, args.output, args.resume)


if __name__ == "__main__":
    main()
