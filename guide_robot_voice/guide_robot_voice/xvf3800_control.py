#!/usr/bin/env python3
"""Чтение DOA (направление на источник звука) с reSpeaker XVF3800.

Без pyusb: используется USBDEVFS_CONTROL ioctl через ctypes, поэтому на
роботе не нужны никакие pip-пакеты.

Протокол (XMOS XVF3800 control, vendor requests):
    bmRequestType = 0xC0  (IN | Vendor | Device)
    bRequest      = 0x00
    wValue        = cmdid | 0x80
    wIndex        = resid
    wLength       = len(payload) + 1
Первый байт ответа -- статус: 0 = успех, 64 = retry (повторить через ~15 мс).

Используемые команды:
    DOA_VALUE         resid 20, cmdid 18 -> uint16 angle_deg, uint16 vad
    DOA_VALUE_RADIANS resid 20, cmdid 19 -> float32 angle_rad, uint32 vad
    AEC_AZIMUTH_VALUES resid 33, cmdid 75 -> 4 x float32 (лучи, радианы)

Угол: 0° = перед, 90° = право, 180° = зад, 270° = лево.
Доверять углу можно только при vad != 0.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import struct
import sys
import time

VID, PID = 0x2886, 0x001A
IFACE = 3

STATUS_SUCCESS = 0
STATUS_RETRY = 64

RESID_GPO_SERVICER = 20
RESID_AEC = 33

CMD_DOA_VALUE = 18
CMD_DOA_VALUE_RADIANS = 19
CMD_AEC_AZIMUTH_VALUES = 75


class CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer from <linux/usbdevice_fs.h>."""

    _fields_ = [
        ("bRequestType", ctypes.c_ubyte),
        ("bRequest", ctypes.c_ubyte),
        ("wValue", ctypes.c_ushort),
        ("wIndex", ctypes.c_ushort),
        ("wLength", ctypes.c_ushort),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


# _IOWR('U', 0, struct usbdevfs_ctrltransfer) -> 0xC0185500 on 64-bit Linux
USBDEVFS_CONTROL = 0xC0185500


def find_device():
    """Return the /dev/bus/usb path for the reSpeaker XVF3800."""
    root = "/sys/bus/usb/devices"
    want = f"{VID:04x}:{PID:04x}"
    for entry in sorted(os.listdir(root)):
        node = os.path.join(root, entry)
        try:
            with open(os.path.join(node, "idVendor")) as fh:
                vid = fh.read().strip()
            with open(os.path.join(node, "idProduct")) as fh:
                pid = fh.read().strip()
            with open(os.path.join(node, "busnum")) as fh:
                bus = int(fh.read().strip())
            with open(os.path.join(node, "devnum")) as fh:
                dev = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        if f"{vid}:{pid}" == want:
            return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    raise SystemExit(f"reSpeaker {want} не найден в {root}")


def open_dev(path=None):
    """Open the usbfs node read-write."""
    return os.open(path or find_device(), os.O_RDWR)


def ctrl_in(fd, resid, cmdid, nbytes, retries=5):
    """Vendor control-IN; strips the leading status byte."""
    length = nbytes + 1
    buf = ctypes.create_string_buffer(length)
    for attempt in range(retries):
        xfer = CtrlTransfer(
            0xC0, 0x00, 0x80 | cmdid, resid, length, 1000,
            ctypes.cast(buf, ctypes.c_void_p),
        )
        fcntl.ioctl(fd, USBDEVFS_CONTROL, xfer)
        return_status = buf.raw[0]
        if return_status == STATUS_RETRY:
            time.sleep(0.015)
            continue
        if return_status != STATUS_SUCCESS:
            raise IOError(f"status {return_status:#04x} после {attempt + 1} попыток")
        return buf.raw[1:]
    raise IOError("SERVICER_COMMAND_RETRY не прошёл")


def read_doa(fd):
    """Return (angle_deg, vad) from DOA_VALUE."""
    angle, vad = struct.unpack("<HH", ctrl_in(fd, RESID_GPO_SERVICER, CMD_DOA_VALUE, 4))
    return angle, vad


def scale_rgb(color, brightness=0.6):
    """Scale channel intensity; brightness register does not apply to DOA mode."""
    channels = [round(((color >> shift) & 255) * brightness) for shift in (16, 8, 0)]
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def led_command(fd, cmd, payload):
    """Write one documented LED control command, without saving configuration."""
    buf = ctypes.create_string_buffer(payload, len(payload))
    xfer = CtrlTransfer(0x40, 0, cmd, RESID_GPO_SERVICER, len(payload), 1000,
                        ctypes.cast(buf, ctypes.c_void_p))
    fcntl.ioctl(fd, USBDEVFS_CONTROL, xfer)


def set_direction_colors(fd, base, direction):
    led_command(fd, 17, struct.pack('<II', base, direction))


def configure_direction_led(fd, base=0xE80001, direction=0xFFFFFF, brightness=0.6):
    """Set and verify the normal, RAM-only firmware direction effect."""
    destination = (scale_rgb(base, brightness), scale_rgb(direction, brightness))
    set_direction_colors(fd, *destination)
    led_command(fd, 12, b'\x04')
    actual = struct.unpack('<II', ctrl_in(fd, RESID_GPO_SERVICER, 17, 8))
    mode = ctrl_in(fd, RESID_GPO_SERVICER, 12, 1)[0]
    if actual != destination or mode != 4:
        raise IOError(f'LED readback mismatch: mode={mode}, colors={actual}')


def read_doa_rad(fd):
    """Return (angle_rad, vad) from DOA_VALUE_RADIANS."""
    rad, vad = struct.unpack("<fI", ctrl_in(fd, RESID_GPO_SERVICER, CMD_DOA_VALUE_RADIANS, 8))
    return rad, vad


def read_azimuths(fd):
    """Return the four beam azimuths in radians."""
    return struct.unpack("<4f", ctrl_in(fd, RESID_AEC, CMD_AEC_AZIMUTH_VALUES, 16))


def main() -> int:
    ap = argparse.ArgumentParser(description="Read DOA from reSpeaker XVF3800")
    ap.add_argument("--count", type=int, default=1, help="сколько отсчётов (0 = бесконечно)")
    ap.add_argument("--interval", type=float, default=0.2, help="пауза между отсчётами, с")
    ap.add_argument("--device", default=None, help="путь к /dev/bus/usb/BBB/DDD")
    ap.add_argument("--json", action="store_true", help="вывод в JSON, по объекту на строку")
    ap.add_argument("--beams", action="store_true", help="заодно печатать 4 луча AEC")
    args = ap.parse_args()

    fd = open_dev(args.device)
    try:
        n = 0
        while args.count == 0 or n < args.count:
            angle, vad = read_doa(fd)
            row = {"t": round(time.time(), 3), "doa_deg": angle, "vad": vad}
            if args.beams:
                row["beams_deg"] = [round(a * 57.2957795, 1) for a in read_azimuths(fd)]
            if args.json:
                print(json.dumps(row))
            else:
                extra = ""
                if args.beams:
                    extra = "  beams=" + " ".join(f"{b:6.1f}" for b in row["beams_deg"])
                print(f"DOA {angle:3d}°  vad={vad}{extra}")
            n += 1
            if args.count == 0 or n < args.count:
                time.sleep(args.interval)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
