#!/usr/bin/env bash
# Start or stop this copy of the standalone tracker without touching ROS.
set -eu
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
tracker="$script_dir/sound_head_tracker.py"
pidfile="${FURO_TRACK_PIDFILE:-/tmp/furo-sound-tracker.pid}"
logfile="${FURO_TRACK_LOG:-/tmp/furo-sound-tracker.log}"

is_our_process() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null &&
        [[ -r "/proc/$pid/cmdline" ]] &&
        tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq -- "$tracker"
}

if [[ "${1:-}" == stop ]]; then
    if [[ -f "$pidfile" ]] && is_our_process "$(cat "$pidfile")"; then
        pid="$(cat "$pidfile")"
        kill -TERM "$pid"
        for _ in {1..30}; do
            is_our_process "$pid" || break
            sleep 0.1
        done
        if is_our_process "$pid"; then
            echo "Трекер $pid ещё завершает работу" >&2
            exit 1
        fi
        echo "Трекер остановлен"
    else
        echo "Запущенный этим скриптом трекер не найден"
    fi
    rm -f -- "$pidfile"
    exit 0
fi

if [[ -f "$pidfile" ]] && is_our_process "$(cat "$pidfile")"; then
    echo "Трекер уже работает: PID $(cat "$pidfile"), лог $logfile"
    exit 1
fi

setsid python3 -u "$tracker" "$@" > "$logfile" 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > "$pidfile"
sleep 1
if ! is_our_process "$pid"; then
    echo "Трекер завершился при запуске; лог:" >&2
    cat "$logfile" >&2
    rm -f -- "$pidfile"
    exit 1
fi
echo "Трекер работает: PID $pid, лог $logfile"
