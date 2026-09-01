#!/usr/bin/env bash
# Fifine USB — Pulse off (ROS берёт ALSA). JBL Go 2 — default Pulse sink.
# Запускается user-unit jetson-audio.service (цикл, не oneshot).
#
#   scripts/jetson_audio.sh        # цикл (systemd)
#   scripts/jetson_audio.sh pair   # сканирует ~45 с, сопрягает JBL*
set -u

MIC_CARD="${JETSON_MIC_CARD:-alsa_card.usb-0c76_USB_PnP_Audio_Device-00}"
MAC="${JETSON_BT_MAC:-}"

jbl_mac() {
  if [[ -n "$MAC" ]]; then
    printf '%s\n' "$MAC"
    return 0
  fi
  bluetoothctl devices 2>/dev/null |
    awk 'BEGIN{IGNORECASE=1} $0 ~ /JBL/ { print $2; exit }'
}

a2dp_sink() {
  pactl list short sinks 2>/dev/null | awk '/bluez_sink/ { print $2; exit }'
}

bt_up() {
  rfkill unblock bluetooth 2>/dev/null || true
  bluetoothctl power on >/dev/null 2>&1 || true
}

apply() {
  bt_up
  if pactl list cards short 2>/dev/null | grep -q "$MIC_CARD"; then
    pactl set-card-profile "$MIC_CARD" off 2>/dev/null || true
  fi
  mac="$(jbl_mac)"
  if [[ -n "$mac" ]]; then
    bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  fi
  sink="$(a2dp_sink)"
  [[ -n "$sink" ]] || return 1
  pactl set-default-sink "$sink" || return 1
  pactl list short sink-inputs 2>/dev/null | awk '{print $1}' | while read -r id; do
    pactl move-sink-input "$id" "$sink" 2>/dev/null || true
  done
  return 0
}

cmd_pair() {
  bt_up
  bluetoothctl pairable on >/dev/null 2>&1 || true
  echo "Зажми BT на JBL Go 2, пока светодиод не замигает (~3 с)."
  timeout 1 bluetoothctl scan on >/dev/null 2>&1 || true
  mac=""
  for _ in $(seq 1 45); do
    mac="$(jbl_mac)"
    [[ -n "$mac" ]] && break
    sleep 1
  done
  bluetoothctl scan off >/dev/null 2>&1 || true
  if [[ -z "$mac" ]]; then
    echo "JBL не видна. Сними её с телефона и повтори pair." >&2
    exit 1
  fi
  bluetoothctl pair "$mac"
  bluetoothctl trust "$mac"
  bluetoothctl connect "$mac"
  apply || true
  echo "JBL $mac  sink=$(a2dp_sink)"
}

if [[ "${1:-}" == "pair" ]]; then
  cmd_pair
  exit 0
fi

while true; do
  apply || true
  sleep 8
done
