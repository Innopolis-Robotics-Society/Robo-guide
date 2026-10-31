#!/bin/bash
# Запуск трекинга головы по звуку в фоне, переживая разрыв SSH.
# Отдельный файл, потому что pkill -f по имени скрипта убивает любую
# командную строку, где это имя встречается, включая саму SSH-сессию.

LIMIT="${1:-20}"
LOG=/tmp/track.log
PIDFILE=/tmp/track.pid

pkill -f 'sound_head_tracker\.py' 2>/dev/null
pkill -f 'furo_head_capture\.py' 2>/dev/null
sleep 2

rm -f "$LOG"
cd /home/jetson || exit 1

setsid python3 -u /home/jetson/sound_head_tracker.py --limit "$LIMIT" \
    > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

sleep 4
echo "pid: $(cat "$PIDFILE")"
echo "--- лог ---"
cat "$LOG"
