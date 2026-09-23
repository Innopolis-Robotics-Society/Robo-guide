#!/usr/bin/env python3
"""Лог событий тач-панели и загрузки CPU (шаг 100 мс), время -- CLOCK_MONOTONIC.

Пишется параллельно с ftrace из mon_start.sh, чтобы сопоставлять касания с провалами
в каденции кадров FL2000. Запускать от root (event-узел принадлежит группе input).
"""

from __future__ import annotations

import glob
import os
import select
import struct
import time

EV = struct.Struct("llHHi")  # input_event на aarch64: timeval(2*long), type, code, value
BTN_TOUCH = 0x14A
EV_KEY, EV_ABS = 1, 3


def find_touch() -> str | None:
    """Найти event-узел тач-панели по имени устройства."""
    for path in glob.glob("/sys/class/input/event*/device/name"):
        with open(path, encoding="utf-8") as f:
            if "Touchscreen" in f.read():
                return "/dev/input/" + path.split("/")[4]
    return None


def cpustat() -> dict[str, tuple[int, int]]:
    """Прочитать /proc/stat: cpuN -> (total, idle+iowait)."""
    result = {}
    with open("/proc/stat", encoding="utf-8") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                fields = line.split()
                values = list(map(int, fields[1:9]))
                result[fields[0]] = (sum(values), values[3] + values[4])
    return result


def main() -> None:
    """Главный цикл: события тача по мере поступления, CPU каждые 100 мс."""
    dev = find_touch()
    print(f"touch device: {dev}", flush=True)
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK) if dev else None
    prev = cpustat()
    last = time.monotonic()
    while True:
        if fd is None:
            time.sleep(0.1)
        elif select.select([fd], [], [], 0.1)[0]:
            try:
                data = os.read(fd, EV.size * 64)
            except OSError as exc:
                print(f"{time.monotonic():.6f} touch read error {exc}", flush=True)
                fd = None
                continue
            for off in range(0, len(data) - EV.size + 1, EV.size):
                sec, usec, ev_type, code, value = EV.unpack_from(data, off)
                if ev_type == EV_KEY and code == BTN_TOUCH:
                    state = "DOWN" if value else "UP"
                    print(
                        f"{time.monotonic():.6f} TOUCH {state} (ev {sec}.{usec:06d})", flush=True
                    )
                elif ev_type == EV_ABS:
                    print(f"{time.monotonic():.6f} abs{code}={value}", flush=True)
        now = time.monotonic()
        if now - last >= 0.1:
            cur = cpustat()
            parts = []
            for name in sorted(cur):
                total = cur[name][0] - prev[name][0]
                idle = cur[name][1] - prev[name][1]
                busy = 100 * (total - idle) / total if total else 0
                parts.append(f"{name[3:]}:{busy:3.0f}")
            prev, last = cur, now
            print(f"{now:.6f} CPU " + " ".join(parts), flush=True)


if __name__ == "__main__":
    main()
