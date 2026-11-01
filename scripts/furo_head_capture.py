#!/usr/bin/env python3
"""Record FURO-D encoders through a Linux USB–DB9 COM adapter, without ROS."""

import argparse
import csv
import fcntl
import json
import math
import os
import select
import statistics
import struct
import sys
import termios
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

QUERY = bytes.fromhex("FF FF 2E 04 03 A0 05 25")
PREFIX = bytes.fromhex("FF FF 2E 0F 47 A0")


class Decoder:
    """Recover telemetry frames across arbitrary reads and corrupt bytes."""

    def __init__(self):
        """Initialize stream state and diagnostics."""
        self.buffer = bytearray()
        self.bad_checksums = 0
        self.discarded_bytes = 0

    def feed(self, chunk):
        """Return complete, checksum-validated 19-byte telemetry frames."""
        self.buffer.extend(chunk)
        frames = []
        while len(self.buffer) >= len(PREFIX):
            if not self.buffer.startswith(PREFIX):
                del self.buffer[0]
                self.discarded_bytes += 1
            elif len(self.buffer) < 19:
                break
            elif sum(self.buffer[2:19]) & 255 != 255:
                self.bad_checksums += 1
                self.discarded_bytes += 1
                del self.buffer[0]
            else:
                frames.append(bytes(self.buffer[:19]))
                del self.buffer[:19]
        return frames


@contextmanager
def open_com(port):
    """Own the COM device at 115200 8N1; keep raw/no-echo settings on exit."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    exclusive = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        exclusive = True
        settings = termios.tcgetattr(fd)
        settings[0] = settings[1] = settings[3] = 0
        settings[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        settings[4] = settings[5] = termios.B115200
        settings[6][termios.VMIN] = settings[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        yield fd
    finally:
        # This is a controller COM port, not an interactive terminal. Restoring
        # its initial cooked settings can re-enable kernel ECHO and echo RX
        # bytes back into the controller outside our explicit write() calls.
        # The raw configuration must therefore remain in place on close.
        try:
            if exclusive:
                fcntl.ioctl(fd, termios.TIOCNXCL)
        finally:
            os.close(fd)


def mark_window(samples, now, window):
    """Summarize recent head samples, rejecting absent or stale feedback."""
    recent = [(t, value) for t, value in samples if now - t <= window]
    if len(recent) < 5 or now - recent[-1][0] > 0.5:
        raise ValueError("Нет свежего окна из 5 кадров; подожди около секунды.")
    if recent[-1][0] - recent[0][0] < window * 0.7:
        raise ValueError("Окно ещё не заполнено; удерживай голову и повтори отметку.")
    values = [value for _, value in recent]
    return {
        "elapsed_s": now,
        "samples": len(values),
        "window_s": window,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "spread": max(values) - min(values),
    }


def capture(args):
    """Record raw bytes, validated CSV samples and manually marked positions."""
    args.output.mkdir(parents=True, exist_ok=False)
    decoder = Decoder()
    samples = deque()
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "port": args.port,
        "resolved_port": os.path.realpath(args.port),
        "baud": 115200,
        "query_hex": QUERY.hex(" "),
        "passive": args.passive,
        "head_field": "offset 14, 4 bytes little-endian; decoded as int32",
        "commands_sent": 0,
        "frames": 0,
        "marks": {},
        "status": "starting",
    }
    start = time.monotonic()

    def save():
        report.update(
            elapsed_s=time.monotonic() - start,
            bad_checksums=decoder.bad_checksums,
            discarded_bytes=decoder.discarded_bytes,
            trailing_bytes=len(decoder.buffer),
        )
        marks = report["marks"]
        if "left" in marks and "right" in marks:
            left, right = marks["left"]["median"], marks["right"]["median"]
            report["endpoints"] = {
                "left": left, "right": right, "span": abs(right - left),
                "midpoint": (left + right) / 2,
                "increases_toward": "right" if right > left else "left" if left > right
                else "undetermined",
            }
        temporary = args.output / "summary.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.output / "summary.json")

    print(f"COM: {args.port}, 115200 8N1. Запись: {args.output}", flush=True)
    print(
        "Пассивный режим: байты в COM не отправляются."
        if args.passive else "Отправляется только запрос телеметрии; команд движения нет.",
        flush=True,
    )
    print("Удерживай положение 1 с, затем буква + Enter:", flush=True)
    print("l — левый предел; r — правый; c — центр; q — сохранить и выйти.", flush=True)
    stdin = sys.stdin if sys.stdin.isatty() else None
    last_frame = start
    next_query = next_display = start
    try:
        with open_com(args.port) as fd, (args.output / "rx.bin").open("wb") as raw:
            with (args.output / "samples.csv").open("w", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow([
                    "elapsed_s", "received_unix_s", "wheel_1", "wheel_2",
                    "head_encoder", "frame_hex",
                ])
                report["status"] = "recording"
                save()
                while True:
                    now = time.monotonic()
                    if args.duration and now - start >= args.duration:
                        break
                    if not args.passive and now >= next_query:
                        if os.write(fd, QUERY) != len(QUERY):
                            raise OSError("Не удалось отправить полный запрос телеметрии")
                        report["commands_sent"] += 1
                        next_query = now + 0.2
                    ready, _, _ = select.select([fd] + ([stdin] if stdin else []), [], [], 0.05)
                    if fd in ready:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            raise OSError("COM-порт отключён или закрылся")
                        raw.write(chunk)
                        raw.flush()
                        for frame in decoder.feed(chunk):
                            now = last_frame = time.monotonic()
                            wheel_1, wheel_2, head = struct.unpack_from("<iii", frame, 6)
                            elapsed = now - start
                            samples.append((elapsed, head))
                            while samples and elapsed - samples[0][0] > args.window:
                                samples.popleft()
                            writer.writerow([
                                f"{elapsed:.6f}", f"{time.time():.6f}", wheel_1,
                                wheel_2, head, frame.hex(" "),
                            ])
                            csv_file.flush()
                            report["frames"] += 1
                            report["head_min"] = min(head, report.get("head_min", head))
                            report["head_max"] = max(head, report.get("head_max", head))
                    if stdin and stdin in ready:
                        line = stdin.readline()
                        command = line.strip().lower()
                        if not line or command == "q":
                            break
                        names = {"l": "left", "r": "right", "c": "center"}
                        if command in names:
                            try:
                                mark = mark_window(samples, time.monotonic() - start, args.window)
                                report["marks"][names[command]] = mark
                                save()
                                print(f"{names[command]}: {mark}", flush=True)
                            except ValueError as error:
                                print(error, flush=True)
                    now = time.monotonic()
                    if now - last_frame > args.timeout:
                        raise TimeoutError(
                            "Нет валидных ответов контроллера; проверь COM и питание"
                        )
                    if now >= next_display:
                        if samples:
                            print(
                                f"head={samples[-1][1]}  "
                                f"min/max={report['head_min']}/{report['head_max']}  "
                                f"frames={report['frames']} bad_crc={decoder.bad_checksums}",
                                flush=True,
                            )
                        next_display = now + 0.5
        if not report["frames"]:
            raise RuntimeError("Запись завершена без валидных кадров")
        report["status"] = "finished"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except Exception as error:
        report["status"] = "error"
        report["error"] = str(error)
        raise
    finally:
        save()
        print(f"Результат: {args.output / 'summary.json'}", flush=True)
        if "endpoints" in report:
            print(json.dumps(report["endpoints"], ensure_ascii=False), flush=True)
        if not {"left", "right"} <= report["marks"].keys():
            print("Оба предела ещё не отмечены; калибровка не завершена.", flush=True)


def main():
    """Parse command-line options and report COM errors without a traceback."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/tty_motors")
    parser.add_argument(
        "--passive", action="store_true", help="receive only; send no bytes to the COM port"
    )
    parser.add_argument("--output", type=Path, default=Path(
        "head-capture-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    ))
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 = until q/Ctrl+C")
    parser.add_argument(
        "--timeout", type=float, default=5, help="max seconds without valid frames"
    )
    parser.add_argument("--window", type=float, default=1, help="seconds per endpoint sample")
    args = parser.parse_args()
    if not all(math.isfinite(v) for v in (args.duration, args.timeout, args.window)):
        parser.error("Времена должны быть конечными числами")
    if args.duration < 0 or args.timeout <= 0 or args.window < 0.2:
        parser.error("Нужны duration >= 0, timeout > 0, window >= 0.2")
    if not sys.stdin.isatty() and not args.duration:
        parser.error("Для отметок нужен интерактивный терминал; либо укажи --duration")
    try:
        capture(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
