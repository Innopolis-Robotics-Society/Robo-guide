#!/usr/bin/env bash
# Точка доступа на Wi-Fi НОУТА. Джетсон в металле — клиент, 10.42.0.2.
#
#   sudo scripts/laptop_hotspot.sh up
#   sudo scripts/laptop_hotspot.sh down
#
# Профиль AP: guide-robot-ap (не путать с STA «guide-robot» от робота).
set -eu

SSID="${GUIDE_HOTSPOT_SSID:-guide-robot}"
PASS="${GUIDE_HOTSPOT_PASS:-guide-robot}"
CON="${GUIDE_HOTSPOT_CON:-guide-robot-ap}"
PREV_FILE="${GUIDE_HOTSPOT_PREV:-/tmp/guide-robot-laptop-hotspot.prev}"
ROBOT=10.42.0.2

wifi_dev() {
    nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "wifi" { print $1; exit }'
}

wifi_sta() {
    local dev="$1"
    nmcli -t -f NAME,TYPE,DEVICE connection show --active |
        awk -F: -v d="$dev" '$2 == "802-11-wireless" && $3 == d { print $1; exit }'
}

has_con() {
    nmcli -t -f NAME connection show | grep -qx "$1"
}

cmd="${1:-}"
if [[ "$cmd" != "up" && "$cmd" != "down" ]]; then
    echo "usage: $0 up|down" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "нужен root: sudo $0 $cmd" >&2
    exit 1
fi

DEV="$(wifi_dev)"
if [[ -z "$DEV" ]]; then
    echo "нет wifi-интерфейса" >&2
    exit 1
fi

if [[ "$cmd" == "down" ]]; then
    nmcli connection down "$CON" >/dev/null 2>&1 || true
    prev=""
    [[ -f "$PREV_FILE" ]] && prev="$(cat "$PREV_FILE")"
    if [[ -n "$prev" ]]; then
        nmcli connection modify "$prev" connection.autoconnect yes >/dev/null 2>&1 || true
        nmcli device set "$DEV" autoconnect yes 2>/dev/null || true
        nmcli connection up "$prev"
        echo "STA $prev на $DEV"
    else
        echo "AP выключен, сохранённой STA нет"
    fi
    exit 0
fi

sta="$(wifi_sta "$DEV")"
if [[ -n "$sta" && "$sta" != "$CON" ]]; then
    printf '%s\n' "$sta" >"$PREV_FILE"
fi

# GNOME-shell сам возвращает InnoRobotics/108hq и убивает AP (~1 мин).
nmcli device set "$DEV" autoconnect no
while IFS=: read -r name type; do
    [[ "$type" == "802-11-wireless" && "$name" != "$CON" ]] || continue
    nmcli connection modify "$name" connection.autoconnect no >/dev/null 2>&1 || true
done < <(nmcli -t -f NAME,TYPE connection show)
if [[ -n "$sta" && "$sta" != "$CON" ]]; then
    nmcli connection down "$sta" >/dev/null 2>&1 || true
fi

if has_con "$CON"; then
    mode="$(nmcli -g 802-11-wireless.mode connection show "$CON" 2>/dev/null || true)"
    if [[ "$mode" != "ap" ]]; then
        nmcli connection delete "$CON" >/dev/null
    fi
fi

if ! has_con "$CON"; then
    nmcli connection add type wifi ifname "$DEV" con-name "$CON" autoconnect no \
        ssid "$SSID" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$PASS" \
        ipv4.method shared >/dev/null
fi

nmcli connection up "$CON"

ip="$(ip -4 -o addr show dev "$DEV" | awk '{ print $4 }' | head -1)"
echo "SSID=$SSID  pass=$PASS  ноут ${ip:-10.42.0.1}  робот $ROBOT"
echo "Держу точку 3 мин (GNOME иначе вернёт офисный Wi-Fi)..."
for _ in $(seq 1 90); do
    active="$(nmcli -t -f NAME connection show --active | head -5 || true)"
    if ! printf '%s\n' "$active" | grep -qx "$CON"; then
        echo "точку сбили, поднимаю снова"
        nmcli connection up "$CON" || true
    fi
    if ping -c 1 -W 1 "$ROBOT" >/dev/null 2>&1; then
        echo "робот $ROBOT отвечает.  ssh -Y jetson@$ROBOT"
        exit 0
    fi
    sleep 2
done
echo "робот не подключился. Не трогай меню Wi-Fi. На Jetson: journalctl -u guide-hotspot-client -n 30"
