#!/usr/bin/env python3
"""Загрузить только два feature-модуля openWakeWord, без TTS и готовых wake words."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    """Скачать фиксированные upstream ONNX-модели в отдельную папку эксперимента."""
    import requests

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    models = []
    for name in ("melspectrogram.onnx", "embedding_model.onnx"):
        url = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/" + name
        path = args.output / name
        if not path.exists():
            print(f"Download: {url}", flush=True)
            response = requests.get(url, stream=True, timeout=(15, 120))
            response.raise_for_status()
            temporary = path.with_suffix(".download")
            with temporary.open("xb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    stream.write(chunk)
            temporary.rename(path)
        models.append(
            {
                "file": name,
                "url": url,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (args.output / "provenance.json").write_text(json.dumps(models, indent=2), encoding="utf-8")
    print(json.dumps(models, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
