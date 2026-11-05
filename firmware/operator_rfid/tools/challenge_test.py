#!/usr/bin/env python3
"""Проверка моста rfid_bridge без ROS (design E4, критерии 15-17).

    pip install pyserial
    ./tools/challenge_test.py                 # /dev/rfid0
    ./tools/challenge_test.py /dev/ttyACM0    # до установки udev-правила

Опрос идёт в цикле: прошивка делает ровно один REQA на challenge, и
попадание карты в поле вероятностное -- на живом FM17522 наблюдалось
3 подряд no_card перед успехом. Веди картой над ридером, пока скрипт
работает.
"""
import hashlib
import hmac
import json
import os
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/rfid0"
NONCE = "00" * 32
ATTEMPTS = 20
SECRET_FILE = os.path.expanduser("~/.config/guide_robot/rfid_secret")

secret = open(SECRET_FILE).read().strip()

s = serial.Serial(PORT, 115200, timeout=2)
time.sleep(2)  # прошивка ждёт открытия порта в setup()
s.reset_input_buffer()
print(f"порт {PORT}, веди картой над ридером...")

for i in range(ATTEMPTS):
    s.write(json.dumps({"cmd": "challenge", "nonce": NONCE}).encode() + b"\n")
    line = s.readline().decode(errors="replace").strip()
    if not line:
        print(f"{i:2d} <таймаут, ответа нет>")
        continue
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        print(f"{i:2d} не JSON: {line!r}")
        continue

    if not r.get("ok"):
        print(f"{i:2d} {r.get('err')}")
        time.sleep(0.4)
        continue

    expect = hmac.new(secret.encode(), NONCE.encode(), hashlib.sha256).hexdigest()
    extra = set(r) - {"ok", "resp", "card"}
    print()
    print(f"card : {r.get('card')}")
    print("HMAC : " + ("совпал" if r.get("resp") == expect else "РАЗОШЁЛСЯ"))
    print("E4   : " + (f"НАРУШЕН, лишние поля: {sorted(extra)}" if extra else "ок"))
    break
else:
    print(f"\n{ATTEMPTS} попыток без успеха.")
    print("Если карта прошита -- сверь ключи:")
    print("  grep -A2 SECTOR_KEY rfid_bridge/include/rfid_bridge_secrets.h")
    print("  cat ~/.config/guide_robot/rfid_sector_key")
