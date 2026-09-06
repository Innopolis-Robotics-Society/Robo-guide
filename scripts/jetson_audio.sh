#!/usr/bin/env bash
# Fifine USB — Pulse off (ROS берёт ALSA). Behringer UM2 — default Pulse sink, 20%.
# Запускается user-unit jetson-audio.service (цикл, не oneshot).
#
#   scripts/jetson_audio.sh        # цикл (systemd)
# Колонка громкая: не поднимать VOLUME выше 20% без явной просьбы.
set -u

MIC_CARD="${JETSON_MIC_CARD:-alsa_card.usb-0c76_USB_PnP_Audio_Device-00}"
# UM2 в ALSA/Pulse — Burr-Brown TI USB Audio CODEC, не строка «UM2».
SPK_MATCH="${JETSON_SPK_MATCH:-UM2|BEHRINGER|Burr-Brown|USB_Audio_CODEC}"
VOLUME="${JETSON_SPK_VOLUME:-20%}"

um2_sink() {
  if [[ -n "${JETSON_SPK_SINK:-}" ]]; then
    printf '%s\n' "$JETSON_SPK_SINK"
    return 0
  fi
  pactl list short sinks 2>/dev/null |
    awk -v re="$SPK_MATCH" 'BEGIN{IGNORECASE=1} $0 ~ re { print $2; exit }'
}

apply() {
  if pactl list cards short 2>/dev/null | grep -q "$MIC_CARD"; then
    pactl set-card-profile "$MIC_CARD" off 2>/dev/null || true
  fi
  sink="$(um2_sink)"
  [[ -n "$sink" ]] || return 1
  pactl set-sink-volume "$sink" "$VOLUME" || return 1
  pactl set-sink-mute "$sink" 0 || true
  pactl set-default-sink "$sink" || return 1
  pactl list short sink-inputs 2>/dev/null | awk '{print $1}' | while read -r id; do
    pactl move-sink-input "$id" "$sink" 2>/dev/null || true
  done
  return 0
}

while true; do
  apply || true
  sleep 8
done
