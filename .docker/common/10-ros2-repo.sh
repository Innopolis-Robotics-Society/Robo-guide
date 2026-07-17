#!/usr/bin/env bash
# Add the ROS 2 apt repository and install ros-base.
# Platform-independent (repo has both amd64 and arm64).
# Uses the modern keyring method (NOT deprecated apt-key).
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${ROS_DISTRO:?ROS_DISTRO must be set}"

apt-get update
apt-get install -y --no-install-recommends \
    curl gnupg2 lsb-release ca-certificates

# Modern signed-by keyring approach
curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc \
    | gpg --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/ros2.list

apt-get update
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-ros-base \
    python3-rosdep python3-colcon-common-extensions

rm -rf /var/lib/apt/lists/*
