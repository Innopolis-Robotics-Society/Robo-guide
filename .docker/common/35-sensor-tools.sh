#!/usr/bin/env bash
# Sensor packages: sllidar_ros2 + ros2_laser_scan_merger.
#
# All ROS 2 dependencies for these packages are already installed in
# 20-ros2-packages.sh, so rosdep is NOT needed here.
#
# Strategy:
#   1. Try apt for sllidar_ros2 (works on amd64; misses on l4t-jetpack base).
#   2. Clone from source if apt misses.
#   3. ros2_laser_scan_merger is always built from source (no apt package).
#   4. Build both with colcon and install into an isolated prefix
#      (/opt/ros/sensors) to avoid overwriting the system setup scripts.
set -exo pipefail   # no -u: ROS setup.sh uses unbound variables internally

: "${ROS_DISTRO:?ROS_DISTRO must be set}"

apt-get update

SENSOR_WS=/tmp/sensor_ws
mkdir -p "${SENSOR_WS}/src"

# ── sllidar_ros2 ──────────────────────────────────────────────────────────────
if apt-get install -y --no-install-recommends "ros-${ROS_DISTRO}-sllidar-ros2" 2>/dev/null; then
    echo "[sensor-tools] sllidar_ros2 installed via apt"
else
    echo "[sensor-tools] sllidar_ros2: apt miss — cloning from source"
    git clone --depth 1 --branch main \
        https://github.com/Slamtec/sllidar_ros2.git \
        "${SENSOR_WS}/src/sllidar_ros2"
fi

# ── ros2_laser_scan_merger (always from source — no apt package) ──────────────
git clone --depth 1 --branch main \
    https://github.com/mich1342/ros2_laser_scan_merger.git \
    "${SENSOR_WS}/src/ros2_laser_scan_merger"

git clone --depth 1 --branch humble \
    https://github.com/pradyum/dual_laser_merger.git \
    "${SENSOR_WS}/src/dual_laser_merger"

# ── Build & install into the system ROS prefix ───────────────────────────────
# Only runs if at least one package was cloned (sllidar via apt skips src/).
if [ -n "$(ls -A "${SENSOR_WS}/src")" ]; then
    set +u
    . /opt/ros/${ROS_DISTRO}/setup.sh
    set -u

    cd "${SENSOR_WS}"

    # No rosdep — all deps already in the image from 20-ros2-packages.sh:
    #   rclcpp, sensor_msgs, std_msgs, tf2_ros, laser_geometry,
    #   pointcloud_to_laserscan, ament_cmake, etc.
    colcon build \
        --merge-install \
        --install-base "/opt/ros/sensors" \
        --cmake-args -DCMAKE_BUILD_TYPE=Release
fi

# ── Clean up ──────────────────────────────────────────────────────────────────
cd /
rm -rf "${SENSOR_WS}"
rm -rf /var/lib/apt/lists/*
