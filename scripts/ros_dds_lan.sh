#!/usr/bin/env bash
# Jazzy-ноут <-> Humble-робот по Wi-Fi (multicast режется, ping живой).
# FastDDS+LOCALHOST не работает: Jazzy шлёт не на 7400 и анонсирует
# FlClashX/docker IP. Cyclone + unicast peer + whitelist wifi — работает.
#
# Пир: аргумент-IP, иначе робот-STA 10.42.0.2, робот-AP 10.42.0.1, офис .167.
#
#   source /opt/ros/jazzy/setup.bash
#   source scripts/ros_dds_lan.sh          # или IP робота
#   ros2 topic list                        # node list пустой — норма для Humble↔Jazzy
#   rviz2 -d guide_robot_bringup/rviz/hardware.rviz
#
# Тот же шелл, что ros2/rviz2. На роботе launch_rviz:=false.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "source $0 [jetson_ip]" >&2
    exit 1
fi

if ! ros2 pkg prefix rmw_cyclonedds_cpp >/dev/null 2>&1; then
    echo "нужен ros-jazzy-rmw-cyclonedds-cpp" >&2
    return 1 2>/dev/null || exit 1
fi

HOTSPOT_STA=10.42.0.2
HOTSPOT_AP=10.42.0.1
LAB=10.100.20.167
PEER=""
# $1 только если это IP — иначе чужие позиционные аргументы шелла.
if [[ "${1:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    PEER="$1"
else
    for cand in "$HOTSPOT_STA" "$HOTSPOT_AP" "$LAB"; do
        if ping -c 1 -W 1 "$cand" >/dev/null 2>&1; then
            PEER="$cand"
            break
        fi
    done
    PEER="${PEER:-$HOTSPOT_STA}"
fi
LOCAL="$(ip -4 route get "$PEER" 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
if [[ -z "$LOCAL" ]]; then
    echo "нет маршрута до $PEER" >&2
    return 1 2>/dev/null || exit 1
fi
if ! ping -c 1 -W 1 "$PEER" >/dev/null 2>&1; then
    echo "робот $PEER не пингуется — ноут AP + jetson client?" >&2
    return 1 2>/dev/null || exit 1
fi

XML="${TMPDIR:-/tmp}/guide_robot_dds_lan.xml"
cat >"$XML" <<EOF
<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain id="any">
    <General>
      <Interfaces>
        <NetworkInterface address="$LOCAL"/>
      </Interfaces>
    </General>
    <Discovery>
      <ParticipantIndex>auto</ParticipantIndex>
      <Peers>
        <Peer address="$PEER"/>
      </Peers>
    </Discovery>
  </Domain>
</CycloneDDS>
EOF

export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="file://$XML"
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS FASTRTPS_DEFAULT_PROFILES_FILE
# SUBNET+multicast на этой сети мёртв; пиры заданы в XML.
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET

if command -v ros2 >/dev/null; then
    ros2 daemon stop >/dev/null 2>&1 || true
fi

echo "Cyclone $LOCAL -> $PEER  ($XML, daemon reset)"
echo "ros2 topic list / rviz2 из этого шелла. ros2 node list будет пустой (Humble vs Jazzy)."
