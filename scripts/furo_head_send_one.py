#!/usr/bin/env python3
"""Одноразовая отправка ОДНОГО подтверждённого пакета головы FURO-D.

Ограничения намеренные:
  * один кадр за запуск, повторов нет;
  * hex принимается только целиком и валидируется (длина LEN+4, checksum,
    инструкция 0x83, регистр 0x1E, ось 0x2B) -- чужая раскладка не пройдёт;
  * требуется --yes, иначе только печатает;
  * после записи порт закрывается, никакого циклического опроса.

Это НЕ замена furo_head_capture.py: тот только читает телеметрию.
"""
from __future__ import annotations

import argparse
import sys

from furo_head_packet import validate_head_packet


def parse_and_validate(hex_text: str) -> bytes:
    parts = hex_text.split()
    try:
        data = bytes(int(p, 16) for p in parts)
        return validate_head_packet(data)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send exactly one validated FURO-D head packet; never loops.")
    ap.add_argument("--hex", required=True, help='кадр целиком, например "FF FF FE 09 83 1E 04 2B EA 06 0A 00 2E"')
    ap.add_argument("--port", default="/dev/tty_motors")
    ap.add_argument("--yes", action="store_true", help="без этого флага в порт ничего не пишется")
    args = ap.parse_args()

    data = parse_and_validate(args.hex)
    pos = data[8] | (data[9] << 8)
    spd = data[10] | (data[11] << 8)

    print(f"порт : {args.port}")
    print(f"кадр : {' '.join(f'{b:02X}' for b in data)}")
    print(f"ось  : 0x{data[7]:02X}, позиция {pos}, скорость {spd}")
    print(f"байт : {len(data)}, checksum OK")

    if not args.yes:
        print("\n--yes не указан: в порт НЕ отправлено.")
        return 0

    # Импорт здесь, чтобы --help и валидация работали без pyserial.
    import serial

    with serial.Serial(args.port, 115200, timeout=0.2) as port:
        port.write(data)
        port.flush()
    print("\nотправлено 1 кадр, порт закрыт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
