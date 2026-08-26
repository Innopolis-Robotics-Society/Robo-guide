#!/usr/bin/env bash
# Unicast DDS: Wi-Fi режет multicast, ping/SSH при этом живые.
# Source в том же шелле, что ros2/rviz2. Только ноут (Jazzy) —
# Humble на роботе отвечает на unicast PDP без XML.
#
#   source /opt/ros/jazzy/setup.bash
#   source scripts/ros_dds_lan.sh          # или IP робота
#   ros2 topic list
#   rviz2 -d guide_robot_bringup/rviz/hardware.rviz
#
# На роботе: launch_rviz:=false — не гонять rviz2 по SSH.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "source $0 [jetson_ip]" >&2
    exit 1
fi

export ROS_STATIC_PEERS="${1:-10.100.20.133}"
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
unset ROS_LOCALHOST_ONLY

# Демон поднимается без этих переменных и дальше отдаёт пустой граф.
if command -v ros2 >/dev/null; then
    ros2 daemon stop >/dev/null 2>&1 || true
fi

echo "ROS_STATIC_PEERS=$ROS_STATIC_PEERS  RANGE=$ROS_AUTOMATIC_DISCOVERY_RANGE (daemon reset)"
