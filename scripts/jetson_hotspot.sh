#!/usr/bin/env bash
# Хост orin, не docker.
#
#   sudo scripts/jetson_hotspot.sh up      # AP на USB-антенне, 10.42.0.1
#                                          # бортовой Wi-Fi остаётся в офисе
#   sudo scripts/jetson_hotspot.sh down    # выключить USB-AP
#   sudo scripts/jetson_hotspot.sh client  # STA к AP ноута, 10.42.0.2
#
# На ноуте: nmcli connection up guide-robot
# Потом:    ssh -Y jetson@10.42.0.1
set -eu

SSID="${GUIDE_HOTSPOT_SSID:-guide-robot}"
PASS="${GUIDE_HOTSPOT_PASS:-guide-robot}"
CON_AP="${GUIDE_HOTSPOT_CON:-guide-robot-usb}"
CON_STA="${GUIDE_HOTSPOT_STA:-guide-robot-sta}"
PREV_FILE="${GUIDE_HOTSPOT_PREV:-/tmp/guide-robot-hotspot.prev}"
KO="${GUIDE_8821AU_KO:-/home/jetson/src/8821au-20210708/8821au.ko}"
ROBOT_STA=10.42.0.2/24
GW=10.42.0.1

wifi_dev() {
    nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "wifi" { print $1; exit }'
}

wifi_usb() {
    local d
    for d in $(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "wifi" { print $1 }'); do
        case "$(readlink -f "/sys/class/net/$d")" in
            */usb*) printf '%s\n' "$d"; return 0 ;;
        esac
    done
    return 1
}

wifi_sta() {
    local dev="$1"
    nmcli -t -f NAME,TYPE,DEVICE connection show --active |
        awk -F: -v d="$dev" '$2 == "802-11-wireless" && $3 == d { print $1; exit }'
}

has_con() {
    nmcli -t -f NAME connection show | grep -qx "$1"
}

remember_sta() {
    local dev="$1"
    local sta
    sta="$(wifi_sta "$dev")"
    if [[ -n "$sta" && "$sta" != "$CON_AP" && "$sta" != "$CON_STA" ]]; then
        printf '%s\n' "$sta" >"$PREV_FILE"
    fi
}

ensure_usb() {
    if wifi_usb >/dev/null; then
        wifi_usb
        return 0
    fi
    if [[ ! -f "$KO" ]]; then
        echo "нет USB wifi и нет $KO" >&2
        exit 1
    fi
    insmod "$KO"
    sleep 2
    wifi_usb || {
        echo "USB wifi не поднялся после insmod" >&2
        exit 1
    }
}

cmd="${1:-}"
if [[ "$cmd" != "up" && "$cmd" != "down" && "$cmd" != "client" ]]; then
    echo "usage: $0 up|down|client" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "нужен root: sudo $0 $cmd" >&2
    exit 1
fi

if [[ "$cmd" == "down" ]]; then
    nmcli connection down "$CON_AP" >/dev/null 2>&1 || true
    nmcli connection down Hotspot >/dev/null 2>&1 || true
    nmcli connection down "$CON_STA" >/dev/null 2>&1 || true
    echo "USB-AP выключен, бортовой Wi-Fi не трогал"
    exit 0
fi

if [[ "$cmd" == "client" ]]; then
    DEV="$(wifi_dev)"
    if [[ -z "$DEV" ]]; then
        echo "нет wifi-интерфейса" >&2
        exit 1
    fi
    remember_sta "$DEV"
    if ! has_con "$CON_STA"; then
        nmcli connection add type wifi con-name "$CON_STA" ifname "$DEV" ssid "$SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" wifi-sec.psk-flags 0 \
            ipv4.method manual ipv4.addresses "$ROBOT_STA" ipv4.gateway "$GW" ipv4.dns "$GW" \
            connection.autoconnect no >/dev/null
    else
        nmcli connection modify "$CON_STA" \
            connection.autoconnect no \
            802-11-wireless.ssid "$SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" wifi-sec.psk-flags 0 \
            ipv4.method manual \
            ipv4.addresses "$ROBOT_STA" \
            ipv4.gateway "$GW" \
            ipv4.dns "$GW"
    fi

    if [[ "${GUIDE_HOTSPOT_WAIT_BG:-}" != "1" ]]; then
        systemctl stop guide-hotspot-client 2>/dev/null || true
        systemctl reset-failed guide-hotspot-client 2>/dev/null || true
        systemd-run --unit=guide-hotspot-client --collect \
            -E GUIDE_HOTSPOT_WAIT_BG=1 \
            "$0" client
        echo "Жду SSID $SSID до 3 мин. Это не выход — лог ниже."
        echo "На ноуте (другой терминал): sudo scripts/laptop_hotspot.sh up"
        echo "Этот SSH можно закрыть: wait в systemd, не в сессии."
        sleep 0.3
        exec journalctl -u guide-hotspot-client -n 20 -f --no-hostname
    fi

    echo "$(date -Is) жду $SSID"
    for _ in $(seq 1 90); do
        nmcli device wifi rescan >/dev/null 2>&1 || true
        sleep 2
        if nmcli -t -f SSID device wifi list ifname "$DEV" | grep -Fxq "$SSID"; then
            nmcli connection down "$CON_AP" >/dev/null 2>&1 || true
            nmcli connection delete "$CON_STA" >/dev/null 2>&1 || true
            nmcli device wifi connect "$SSID" password "$PASS" ifname "$DEV" name "$CON_STA"
            nmcli connection modify "$CON_STA" \
                wifi-sec.psk "$PASS" wifi-sec.psk-flags 0 \
                ipv4.method manual ipv4.addresses "$ROBOT_STA" \
                ipv4.gateway "$GW" ipv4.dns "$GW" connection.autoconnect no
            nmcli connection up "$CON_STA"
            echo "$(date -Is) STA $SSID  $DEV $ROBOT_STA  ssh jetson@10.42.0.2"
            exit 0
        fi
    done
    echo "$(date -Is) не нашёл $SSID — остаюсь в офисной сети" >&2
    exit 1
fi

# up — AP на USB-антенне, бортовой Wi-Fi не трогаем
DEV="$(ensure_usb)"
nmcli connection down "$CON_STA" >/dev/null 2>&1 || true
if has_con "$CON_AP"; then
    nmcli connection up "$CON_AP"
else
    nmcli device wifi hotspot ifname "$DEV" ssid "$SSID" password "$PASS" band bg
    if has_con Hotspot && ! has_con "$CON_AP"; then
        nmcli connection modify Hotspot connection.id "$CON_AP"
    fi
    nmcli connection modify "$CON_AP" connection.autoconnect no
    nmcli connection modify "$CON_AP" 802-11-wireless.ssid "$SSID"
fi

ip="$(ip -4 -o addr show dev "$DEV" | awk '{ print $4 }' | head -1)"
echo "SSID=$SSID  pass=$PASS  $DEV ${ip:-10.42.0.1}"
echo "На ноуте: nmcli connection up guide-robot"
echo "SSH: ssh -Y jetson@10.42.0.1"
