#!/usr/bin/env python3
"""Pre-flight проверка USB-serial портов робота с автосбросом зависшего хаба.

Зачем. На Jetson (tegra-xusb, 5.15) full-speed устройства за дешёвым USB2-хабом с одним
Transaction Translator (Terminus FE1.1s, Genesys GL850 -- все хабы на роботе такие) периодически
«зависают»: сам хаб отвечает, а все устройства за ним перестают отвечать даже на GET_DESCRIPTOR.
В dmesg это ``cp210x ttyUSBx: failed set request 0x0 status: -110`` /
``cp210x_open - Unable to enable UART`` /
``ch341-uart ttyUSBx: failed to read modem status: -110``,
у sllidar -- ``Error, unexpected error, code: 80008004``, у моторов -- «Не удалось открыть порт».
Воспроизводимый триггер (19.09.2026): два CP2102 за одним хабом одновременно делают
open/close (Ctrl-C стека -- оба sllidar_node закрываются разом). Лечится только
переэнумерацией хаба: физически передёрнуть кабель или USBDEVFS_RESET на сам хаб.

Что делает скрипт. Последовательно (никакой параллельности -- она и есть триггер) открывает
каждый порт и закрывает. Если открытие падает или висит дольше ``--timeout`` -- сбрасывает
родительский внешний хаб (или само устройство, если оно воткнуто прямо в порт Jetson), ждёт,
пока udev вернёт симлинки, и проверяет ещё раз. Ничего не «чинит» молча: каждый шаг в stdout.

Запуск: ``ros2 run guide_robot_bringup usb_serial_preflight`` (или из hardware.launch.py до
старта ros2_control и лидаров). Сброс требует root: без него скрипт сам перезапускает шаг
сброса через ``sudo -n``.

Без rclpy -- модуль тестируется без ROS.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

USBDEVFS_RESET = 0x5514
DEFAULT_PORTS = (
    "/dev/tty_motors",
    "/dev/tty_sonar",
    "/dev/tty_lidar_left",
    "/dev/tty_lidar_right",
)
SYSFS_TTY = Path("/sys/class/tty")
SYSFS_USB = Path("/sys/bus/usb/devices")
LOG_PREFIX = "[usb_preflight]"


def log(msg: str) -> None:
    """Печатает строку с префиксом и сразу сбрасывает буфер (launch читает через pipe)."""
    print(f"{LOG_PREFIX} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Чистые функции (тестируются без железа)
# ---------------------------------------------------------------------------


def parent_usb_path(usb_path: str) -> str | None:
    """``"1-2.1.4"`` -> ``"1-2.1"``, ``"1-2"`` -> ``"usb1"``, ``"usb1"`` -> ``None``."""
    if usb_path.startswith("usb"):
        return None
    head, sep, _ = usb_path.rpartition(".")
    if sep:
        return head
    bus, _, _ = usb_path.partition("-")
    return f"usb{bus}"


def is_external_hub(usb_path: str) -> bool:
    """Внешний хаб = глубина >= 2 (``1-2.1``); ``1-2`` -- хаб на плате devkit, его не сбрасываем.

    Сброс хаба на плате переэнумерирует всё, включая HDMI-донгл киоска.
    """
    return usb_path.count(".") >= 1


def choose_reset_target(usb_path: str) -> tuple[str, str]:
    """Что сбрасывать для зависшего устройства ``usb_path``.

    Возвращает ``(target, reason)``. Внешний родительский хаб -- проверенный способ; если
    устройство воткнуто прямо в порт Jetson, остаётся только сброс самого устройства.
    """
    parent = parent_usb_path(usb_path)
    if parent is not None and is_external_hub(parent):
        return parent, "родительский внешний хаб"
    return usb_path, "устройство воткнуто прямо в порт Jetson, хаб на плате не трогаем"


# ---------------------------------------------------------------------------
# sysfs / usbfs
# ---------------------------------------------------------------------------


def usb_path_of_tty(port: str) -> str | None:
    """``/dev/tty_lidar_left`` -> ``"1-2.1.4"`` (через /sys/class/tty/ttyUSBn/device).

    ``device`` указывает на usb-serial порт (``.../1-2.1.4/1-2.1.4:1.0/ttyUSB3``); поднимаемся
    по дереву до первого каталога с ``devnum`` -- это и есть USB-устройство.
    """
    real = os.path.realpath(port)
    dev_link = SYSFS_TTY / os.path.basename(real) / "device"
    if not dev_link.exists():
        return None
    node = Path(os.path.realpath(dev_link))
    for candidate in (node, *node.parents):
        if (candidate / "devnum").exists() and (candidate / "busnum").exists():
            return candidate.name
    return None


def usbfs_node(usb_path: str) -> str:
    """Узел usbfs для USBDEVFS_RESET: ``/dev/bus/usb/BBB/DDD``."""
    busnum = int((SYSFS_USB / usb_path / "busnum").read_text())
    devnum = int((SYSFS_USB / usb_path / "devnum").read_text())
    return f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"


def reset_usb_device(usb_path: str) -> bool:
    """USBDEVFS_RESET на устройство/хаб. Нужен root; иначе перезапуск через ``sudo -n``."""
    node = usbfs_node(usb_path)
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", sys.executable, os.path.abspath(__file__), "--reset-usb", usb_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.strip():
                log(f"  sudo: {line.strip()}")
        return proc.returncode == 0
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as exc:
        log(f"  не открыть {node}: {exc.strerror}")
        return False
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as exc:
        log(f"  USBDEVFS_RESET {usb_path} ({node}): {exc.strerror} (errno {exc.errno})")
        return False
    finally:
        os.close(fd)
    log(f"  USBDEVFS_RESET {usb_path} ({node}): ok")
    return True


# ---------------------------------------------------------------------------
# Проверка порта
# ---------------------------------------------------------------------------


def probe_port(port: str, timeout: float) -> tuple[bool, str]:
    """Открыть порт, прочитать/записать termios, закрыть.

    Для cp210x/ch341 open -- это control-запросы к мосту (IFC_ENABLE, modem status); на зависшем
    хабе они отваливаются по -110 через ~5 с или висят. Работа идёт в daemon-потоке, чтобы
    зависший ioctl не подвесил pre-flight целиком.
    """
    result: dict[str, object] = {}

    def work() -> None:
        t0 = time.monotonic()
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                attr = termios.tcgetattr(fd)
                termios.tcsetattr(fd, termios.TCSANOW, attr)
            finally:
                os.close(fd)
            result["ok"] = True
            result["msg"] = f"OK ({time.monotonic() - t0:.2f} с)"
        except OSError as exc:
            result["ok"] = False
            dt = time.monotonic() - t0
            result["msg"] = f"{exc.strerror} (errno {exc.errno}) через {dt:.1f} с"

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, f"висит дольше {timeout:.0f} с"
    return bool(result["ok"]), str(result["msg"])


def wait_for_ports(ports: list[str], settle: float) -> None:
    """Ждёт, пока udev вернёт симлинки после переэнумерации, плюс секунда на settle."""
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if all(os.path.exists(p) for p in ports):
            break
        time.sleep(0.2)
    time.sleep(1.0)


# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------


def run(ports: list[str], timeout: float, allow_reset: bool, settle: float) -> int:
    """Проверить порты, при сбое сбросить хабы и перепроверить. 0 -- всё открывается."""
    log(f"проверка портов: {' '.join(ports)}")
    failed: list[str] = []
    for port in ports:
        if not os.path.exists(port):
            log(f"{port}: нет симлинка (udev не совпал, см. 99-Guide-Robot-devices.rules)")
            failed.append(port)
            continue
        usb_path = usb_path_of_tty(port) or "?"
        ok, msg = probe_port(port, timeout)
        log(f"{port} -> {os.path.basename(os.path.realpath(port))} ({usb_path}): {msg}")
        if not ok:
            failed.append(port)

    if not failed:
        log("все порты открываются")
        return 0
    if not allow_reset:
        log(f"сбой: {' '.join(failed)}; сброс отключён (--no-reset)")
        return 1

    # Один сброс на хаб, даже если за ним несколько зависших портов.
    targets: dict[str, str] = {}
    for port in failed:
        usb_path = usb_path_of_tty(port) if os.path.exists(port) else None
        if usb_path is None:
            continue
        target, reason = choose_reset_target(usb_path)
        targets.setdefault(target, reason)
    if not targets:
        log("сбросить нечего: у сбойных портов нет USB-устройства в sysfs")
        return 1

    for target, reason in targets.items():
        log(f"сброс {target} ({reason})")
        reset_usb_device(target)
    wait_for_ports(failed, settle)

    still_failed: list[str] = []
    for port in failed:
        if not os.path.exists(port):
            log(f"{port}: после сброса симлинк не вернулся")
            still_failed.append(port)
            continue
        ok, msg = probe_port(port, timeout)
        log(f"{port} после сброса: {msg}")
        if not ok:
            still_failed.append(port)
    if still_failed:
        log(f"СБОЙ после сброса: {' '.join(still_failed)} -- передёрнуть USB-кабель хаба вручную")
        return 1
    log("после сброса все порты открываются")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI. ``--reset-usb`` -- внутренний режим для перезапуска под sudo."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ports", nargs="+", default=list(DEFAULT_PORTS))
    parser.add_argument("--timeout", type=float, default=8.0, help="с на открытие одного порта")
    parser.add_argument(
        "--settle", type=float, default=10.0, help="с ожидания симлинков после сброса"
    )
    parser.add_argument(
        "--no-reset", action="store_true", help="только проверить, хабы не трогать"
    )
    parser.add_argument("--reset-usb", metavar="USB_PATH", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.reset_usb:
        return 0 if reset_usb_device(args.reset_usb) else 1
    return run(args.ports, args.timeout, not args.no_reset, args.settle)


if __name__ == "__main__":
    sys.exit(main())
