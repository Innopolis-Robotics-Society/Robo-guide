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

# Разрешённые значения для этой оси: только они попадут в порт.
ALLOWED_INSTR = 0x83
ALLOWED_REG = 0x1E
ALLOWED_AXIS = 0x2B


def parse_and_validate(hex_text: str) -> bytes:
    parts = hex_text.split()
    try:
        data = bytes(int(p, 16) for p in parts)
    except ValueError:
        raise SystemExit("hex содержит не-байты")
    if len(data) < 7:
        raise SystemExit(f"слишком короткий кадр: {len(data)} байт")
    if data[0] != 0xFF or data[1] != 0xFF:
        raise SystemExit("нет заголовка FF FF")
    if data[3] + 4 != len(data):
        raise SystemExit(f"LEN={data[3]} требует {data[3] + 4} байт, пришло {len(data)}")
    if (sum(data[2:]) & 0xFF) != 0xFF:
        raise SystemExit("checksum не сходится")
    if data[4] != ALLOWED_INSTR:
        raise SystemExit(f"инструкция {data[4]:02X}, разрешена только {ALLOWED_INSTR:02X}")
    if data[5] != ALLOWED_REG:
        raise SystemExit(f"регистр {data[5]:02X}, разрешён только {ALLOWED_REG:02X}")
    if data[7] != ALLOWED_AXIS:
        raise SystemExit(f"ось {data[7]:02X}, разрешена только {ALLOWED_AXIS:02X}")
    return data


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
