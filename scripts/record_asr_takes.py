#!/usr/bin/env python3
"""Три дубля одной фразы с Razer Seiren X (тот же hw, что у робота).

Запись — на хосте джетсона, desk.launch должен быть ВЫКЛ (иначе мик занят).
Разбор VAD/GigaAM — в контейнере:  python3 scripts/record_asr_takes.py --analyze DIR
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

PHRASE = "Сколько будет семь на семь"
TAKES = (
    ("01_slow", "медленно и чётко"),
    ("02_normal", "обычным темпом"),
    ("03_fast", "быстро, как обычная речь"),
)
SECONDS = 8
RATE = 16000
CHANNELS = 2


def _razor_hw() -> str:
    listing = subprocess.check_output(["arecord", "-l"], text=True)
    for line in listing.splitlines():
        if "Razer" in line or "Seiren" in line:
            return f"hw:{line.split(':', 1)[0].split()[-1]},0"
    sys.exit(f"Razer не найден:\n{listing}")


def _beep() -> None:
    ogg = "/usr/share/sounds/freedesktop/stereo/complete.oga"
    if Path(ogg).is_file():
        subprocess.run(["paplay", ogg], check=False)
        return
    subprocess.run(
        ["speaker-test", "-t", "sine", "-f", "880", "-l", "1", "-P", "2"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _downmix(stereo: bytes) -> bytes:
    samples = memoryview(stereo).cast("h")
    out = bytearray()
    for i in range(0, len(samples), 2):
        mono = (int(samples[i]) + int(samples[i + 1])) // 2
        out += struct.pack("<h", max(-32768, min(32767, mono)))
    return bytes(out)


def _peak_dbfs(pcm: bytes) -> float:
    if not pcm:
        return -120.0
    samples = memoryview(pcm).cast("h")
    peak = max(abs(s) for s in samples)
    if peak <= 0:
        return -120.0
    return 20.0 * math.log10(peak / 32768.0)


def _write_wav(path: Path, pcm: bytes, channels: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


def cmd_record(out_dir: Path) -> None:
    hw = _razor_hw()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"устройство {hw}, каталог {out_dir}", flush=True)
    print(f"фраза: «{PHRASE}»", flush=True)
    print("после каждого гудка — говори до следующего молчания\n", flush=True)
    time.sleep(3)
    for name, hint in TAKES:
        stereo_path = out_dir / f"{name}_stereo.wav"
        mono_path = out_dir / f"{name}_mono.wav"
        print(f"=== {name}: {hint} ===", flush=True)
        print("гудок через 2 с…", flush=True)
        time.sleep(2)
        _beep()
        print(f"ГОВОРИ ({SECONDS} с)", flush=True)
        try:
            raw = subprocess.check_output(
                [
                    "arecord",
                    "-D",
                    hw,
                    "-f",
                    "S16_LE",
                    "-c",
                    str(CHANNELS),
                    "-r",
                    str(RATE),
                    "-d",
                    str(SECONDS),
                    "-q",
                    "-t",
                    "raw",
                ]
            )
        except subprocess.CalledProcessError as error:
            sys.exit(
                f"arecord не открыл {hw} (код {error.returncode}). "
                "Останови desk.launch — ROS держит карту."
            )
        mono = _downmix(raw)
        _write_wav(stereo_path, raw, CHANNELS)
        _write_wav(mono_path, mono, 1)
        print(f"пик { _peak_dbfs(mono):.1f} dBFS → {mono_path}\n", flush=True)
    print("готово. три mono-wav в", out_dir, flush=True)


def cmd_analyze(out_dir: Path) -> None:
    import numpy as np

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "guide_robot_voice"))
    from guide_robot_voice.lib.asr_model import GigaAmCtc
    from guide_robot_voice.lib.vad_hysteresis import VadHysteresis
    from guide_robot_voice.lib.vad_model import SileroVad

    models = repo / "guide_robot_voice" / "models"
    vad = SileroVad(str(models / "silero_vad.onnx"))
    vad.load()
    asr = GigaAmCtc(
        str(models / "gigaam_v3_ctc_int8.onnx"),
        str(models / "gigaam_v3_ctc_tokens.txt"),
        num_threads=2,
    )
    asr.load()

    window = 512
    for name, _hint in TAKES:
        path = out_dir / f"{name}_mono.wav"
        with wave.open(str(path), "rb") as wav:
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        full = asr.decode(pcm)
        print(f"\n===== {name}  peak={_peak_dbfs(pcm.tobytes()):.1f} dBFS =====")
        print(f"GigaAM целиком: {full.text!r}")

        vad.reset()
        hyst = VadHysteresis(
            enter_threshold=0.45,
            exit_threshold=0.18,
            hangover_ms=2000.0,
            window_ms=1000.0 * window / RATE,
        )
        active_runs: list[tuple[int, int]] = []
        start = None
        timeline = []
        for i in range(0, pcm.size - window + 1, window):
            chunk = pcm[i : i + window]
            prob = vad.process(chunk)
            result = hyst.update(prob)
            mark = "#" if result.active else "."
            timeline.append(mark)
            if result.active and start is None:
                start = i
            if not result.active and start is not None:
                active_runs.append((start, i))
                start = None
        if start is not None:
            active_runs.append((start, pcm.size))

        print("VAD 32мс  #речь .тишина")
        print("".join(timeline))
        if not active_runs:
            print("сегментов нет")
            continue
        for idx, (a, b) in enumerate(active_runs, 1):
            text = asr.decode(pcm[a:b]).text
            print(f"  сегмент {idx}: {a / RATE:.2f}–{b / RATE:.2f} с → {text!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", metavar="DIR", help="разобрать уже записанные mono-wav")
    parser.add_argument(
        "--out",
        default=str(Path.home() / "asr_takes"),
        help="каталог записи (по умолчанию ~/asr_takes)",
    )
    args = parser.parse_args()
    if args.analyze:
        cmd_analyze(Path(args.analyze))
        return
    cmd_record(Path(args.out))


if __name__ == "__main__":
    main()
