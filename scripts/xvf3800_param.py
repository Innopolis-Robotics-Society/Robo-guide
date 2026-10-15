#!/usr/bin/env python3
"""Прочитать или временно изменить параметры DSP XVF3800 по USB (без записи во flash).

Нужен root (USB control) и libusb-1.0; работает и при запущенном стеке.
  sudo python3 scripts/xvf3800_param.py dump
  sudo python3 scripts/xvf3800_param.py get PP_AGCGAIN PP_AGCMAXGAIN
  sudo python3 scripts/xvf3800_param.py set PP_AGCMAXGAIN 10
Значения действуют до отключения питания платы. Номера команд сверены с
чтением заводских значений платы 2.0.6 (MIC_GAIN 90, REF_GAIN 8, AGCMAXGAIN 64).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
import time

VENDOR, PRODUCT = 0x2886, 0x001A
STATUS_BUSY = 64
# имя: (resid, cmd, формат struct)
PARAMS = {
    "AUDIO_MGR_MIC_GAIN": (35, 0, "f"),
    "AUDIO_MGR_REF_GAIN": (35, 1, "f"),
    "AUDIO_MGR_OP_L": (35, 15, "BB"),
    "AUDIO_MGR_OP_R": (35, 19, "BB"),
    "AUDIO_MGR_SYS_DELAY": (35, 26, "i"),
    "AEC_AECCONVERGED": (33, 3, "i"),
    "PP_AGCONOFF": (17, 10, "i"),
    "PP_AGCMAXGAIN": (17, 11, "f"),
    "PP_AGCDESIREDLEVEL": (17, 12, "f"),
    "PP_AGCGAIN": (17, 13, "f"),
    "PP_MIN_NS": (17, 21, "f"),
    "PP_MIN_NN": (17, 22, "f"),
    "PP_ECHOONOFF": (17, 23, "i"),
    "PP_NLATTENONOFF": (17, 27, "i"),
    "PP_DTSENSITIVE": (17, 31, "i"),
}


class Device:
    """Открытый control-интерфейс платы."""

    def __init__(self) -> None:
        """Найти плату по VID/PID; без root libusb её не откроет."""
        self.lib = ctypes.CDLL(ctypes.util.find_library("usb-1.0"))
        lib = self.lib
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open_device_with_vid_pid.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint16,
        ]
        lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint16,
            ctypes.c_uint,
        ]
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        self.context = ctypes.c_void_p()
        if lib.libusb_init(ctypes.byref(self.context)) != 0:
            raise RuntimeError("libusb_init не удался")
        self.handle = lib.libusb_open_device_with_vid_pid(self.context, VENDOR, PRODUCT)
        if not self.handle:
            lib.libusb_exit(self.context)
            raise RuntimeError("XVF3800 не открыт: нужен sudo и подключённая плата")

    def close(self) -> None:
        """Освободить USB."""
        self.lib.libusb_close(self.handle)
        self.lib.libusb_exit(self.context)

    def read(self, name: str) -> tuple:
        """Прочитать значение; плата отвечает «занято», пока не готова."""
        resid, cmd, fmt = PARAMS[name]
        size = struct.calcsize("<" + fmt) + 1
        buffer = (ctypes.c_uint8 * size)()
        for _ in range(100):
            count = self.lib.libusb_control_transfer(
                self.handle, 0xC0, 0, cmd | 0x80, resid, buffer, size, 1000
            )
            if count == size and buffer[0] == 0:
                return struct.unpack("<" + fmt, bytes(buffer)[1:])
            if count < 0 or buffer[0] != STATUS_BUSY:
                break
            time.sleep(0.01)
        raise RuntimeError(f"{name}: чтение не удалось (count={count}, status={buffer[0]})")

    def write(self, name: str, values: list[str]) -> tuple:
        """Записать и дождаться, пока чтение вернёт то же значение."""
        resid, cmd, fmt = PARAMS[name]
        parsed = [float(v) if code == "f" else int(v) for v, code in zip(values, fmt, strict=True)]
        data = struct.pack("<" + fmt, *parsed)
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        sent = self.lib.libusb_control_transfer(
            self.handle, 0x40, 0, cmd, resid, buffer, len(data), 1000
        )
        if sent != len(data):
            raise RuntimeError(f"{name}: запись не удалась ({sent})")
        for _ in range(100):
            current = self.read(name)
            if all(abs(a - b) < 1e-3 for a, b in zip(current, parsed, strict=True)):
                return current
            time.sleep(0.01)
        raise RuntimeError(f"{name}: значение после записи не совпало")


def main(argv: list[str]) -> int:
    """Dump | get ИМЯ... | set ИМЯ ЗНАЧЕНИЕ..."""
    if not argv or argv[0] not in ("dump", "get", "set"):
        print(__doc__)
        print("Параметры:", ", ".join(PARAMS))
        return 2
    device = Device()
    try:
        if argv[0] == "set":
            name = argv[1]
            print(name, *device.write(name, argv[2:]))
            return 0
        for name in PARAMS if argv[0] == "dump" else argv[1:]:
            print(name, *device.read(name))
        return 0
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
