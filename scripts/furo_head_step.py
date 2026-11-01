#!/usr/bin/env python3
"""Стенд: одна команда головы + немедленное чтение энкодера, в одном процессе.

Зачем отдельно от furo_head_capture.py: тот держит порт всё время записи,
поэтому отправить команду параллельно нельзя. Здесь порт берётся один раз,
отправляется ОДИН кадр, затем энкодер опрашивается до остановки.

Ограничения:
  * отправляется только ось 0x2B, инструкция 0x83, регистр 0x1E;
  * hex валидируется (LEN, checksum, ось, инструкция);
  * шаг за запуск один, повторов нет;
  * после работы порт отпускается.

Запуск:
    python3 furo_head_step.py --hex "FF FF ..." --out ~/steps/01 --seconds 6
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from furo_head_capture import QUERY, Decoder, open_com  # noqa: E402
from furo_head_packet import validate_head_packet  # noqa: E402

COEF = 8.7266e-4
ZERO = 1770


def validate(hex_text: str) -> bytes:
    """Reject anything that is not our single known head-position packet."""
    try:
        return validate_head_packet(bytes(int(p, 16) for p in hex_text.split()))
    except ValueError as error:
        raise SystemExit(str(error)) from error


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hex", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--port", default="/dev/tty_motors")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    frame = validate(args.hex)
    target = frame[8] | (frame[9] << 8)
    speed = frame[10] | (frame[11] << 8)

    print(f"кадр   : {' '.join(f'{b:02X}' for b in frame)}")
    print(f"цель   : {target}  скорость {speed}  (ожидаемо {(target - ZERO) / 20:+.1f}° от {ZERO})")
    if not args.yes:
        print("--yes не указан: ничего не отправлено")
        return 0

    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=False)
    dec = Decoder()
    rows = []

    def pump(fd, phase, duration):
        """Write the telemetry query and keep reading until the window is over.

        The controller needs tens of milliseconds to answer, so reading once
        right after the write returns nothing. Poll repeatedly inside the
        window and let the decoder stitch frames across reads.
        """
        deadline = time.monotonic() + duration
        next_write = 0.0
        produced = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_write:
                os.write(fd, QUERY)
                next_write = now + 0.2
            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                chunk = b""
            if chunk:
                for fr in dec.feed(chunk):
                    rows.append((phase, fr, time.monotonic()))
                    produced += 1
            else:
                time.sleep(0.01)
        return produced

    with open_com(args.port) as fd:
        pump(fd, "before", 1.2)
        baseline = [fr for k, fr, _ in rows if k == "before"]

        # ровно один кадр
        os.write(fd, frame)
        print("отправлено 1 кадр")

        pump(fd, "after", args.seconds)

    def head(fr):
        return int.from_bytes(fr[14:18], "little", signed=True)

    after = [head(fr) for k, fr, _ in rows if k == "after"]
    if not after:
        print("нет кадров после отправки", file=sys.stderr)
        return 1

    before_med = statistics.median([head(fr) for fr in baseline]) if baseline else None
    med, lo, hi = statistics.median(after), min(after), max(after)
    after_rows = [(head(fr), ts) for k, fr, ts in rows if k == "after"]
    last_time = after_rows[-1][1]
    tail = [(value, ts) for value, ts in after_rows if last_time - ts <= 1.0]
    tail_values = [value for value, _ in tail]
    summary = {
        "target": target, "speed": speed, "zero_ref": ZERO,
        "before_median": before_med, "after_median": med,
        "after_min": lo, "after_max": hi, "after_spread": hi - lo,
        "after_samples": len(after),
        "delta": (med - before_med) if before_med is not None else None,
        "target_error": med - target,
        "expected_delta": target - (before_med if before_med is not None else 0),
        "settle_s": args.seconds,
        "last_window_s": last_time - tail[0][1],
        "last_window_samples": len(tail_values),
        "last_window_median": statistics.median(tail_values),
        "last_window_spread": max(tail_values) - min(tail_values),
        "last_window_target_error": statistics.median(tail_values) - target,
    }
    with open(os.path.join(out, "samples.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "phase", "head_encoder", "frame_hex"])
        t_start = rows[0][2] if rows else 0.0
        for k, fr, ts in rows:
            w.writerow([f"{ts - t_start:.4f}", k, head(fr), fr.hex(" ")])
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print(f"до      : {before_med}")
    print(f"после   : median={med}  min={lo}  max={hi}  размах={hi - lo}")
    print(f"последнее окно: median={summary['last_window_median']}  "
          f"размах={summary['last_window_spread']}  кадров={len(tail_values)}  "
          f"длительность={summary['last_window_s']:.2f}с (остановка не подтверждена)")
    if before_med is not None:
        print(f"сдвиг   : {med - before_med:+.0f} ед  ({(med - before_med) / 20:+.1f}°)")
    print(f"цель    : {target}  ошибка {med - target:+.0f} ед")
    return 0


if __name__ == "__main__":
    sys.exit(main())
