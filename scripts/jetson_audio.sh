#!/usr/bin/env bash
# Держит Bluetooth-колонку как default sink. Razer — off в Pulse,
# чтобы не воровать выход; ROS открывает карту через ALSA.
#
# Запускается user-unit jetson-audio.service (цикл, не oneshot).
set -u

MAC="${JETSON_BT_MAC:-B8:87:6E:8D:DB:17}"
SINK="${JETSON_BT_SINK:-bluez_sink.B8_87_6E_8D_DB_17.a2dp_sink}"
RAZER_CARD="${JETSON_RAZER_CARD:-alsa_card.usb-Razer_Inc_Razer_Seiren_X_UC2120L01202745-00}"

apply() {
  bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
  pactl list short sinks 2>/dev/null | grep -q "$SINK" || return 1
  pactl set-default-sink "$SINK" || return 1
  pactl list short sink-inputs 2>/dev/null | awk '{print $1}' | while read -r id; do
    pactl move-sink-input "$id" "$SINK" 2>/dev/null || true
  done
  if pactl list cards short 2>/dev/null | grep -q "$RAZER_CARD"; then
    pactl set-card-profile "$RAZER_CARD" off 2>/dev/null || true
  fi
  return 0
}

while true; do
  apply || true
  sleep 8
done
