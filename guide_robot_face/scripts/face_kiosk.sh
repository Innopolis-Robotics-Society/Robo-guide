#!/bin/bash
# Kiosk лица на хосте Jetson (не в docker). face_node слушает :8090
# в контейнере с network_mode: host.
set -euo pipefail
# X screensaver по умолчанию 10 мин (timeout 600) — панель гаснет без ввода.
xset s off 2>/dev/null || true
xset s noblank 2>/dev/null || true
xset -dpms 2>/dev/null || true
URL="${FACE_URL:-http://127.0.0.1:8090}"
# hardware.launch дольше минуты. Не открывать firefox пока :8090 не 200 —
# иначе kiosk навечно сидит на «не удаётся подключиться».
until curl -sf -o /dev/null --max-time 2 "$URL"; do
  sleep 1
done
exec firefox --kiosk --new-instance "$URL"
