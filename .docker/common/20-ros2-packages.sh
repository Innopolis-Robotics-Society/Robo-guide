#!/usr/bin/env bash
# ROS 2 packages: description, image pipeline, navigation, control, gazebo.
# Platform-independent. Gazebo can be skipped on headless targets via WITH_GAZEBO=0.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${ROS_DISTRO:?ROS_DISTRO must be set}"
WITH_GAZEBO="${WITH_GAZEBO:-1}"

apt-get update

# Robot description / visualization / tf
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-tf2-tools \
    ros-${ROS_DISTRO}-robot-state-publisher \
    ros-${ROS_DISTRO}-joint-state-publisher \
    ros-${ROS_DISTRO}-xacro \
    ros-${ROS_DISTRO}-rviz2 \
    ros-${ROS_DISTRO}-rviz-default-plugins \
    ros-${ROS_DISTRO}-urdf \
    ros-${ROS_DISTRO}-urdfdom \
    ros-${ROS_DISTRO}-urdfdom-headers \
    ros-${ROS_DISTRO}-urdf-tutorial \
    ros-${ROS_DISTRO}-hardware-interface \
    ros-${ROS_DISTRO}-rqt-robot-steering \
    ros-${ROS_DISTRO}-rqt-tf-tree

# Image / camera pipeline
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-image-transport \
    ros-${ROS_DISTRO}-image-transport-plugins \
    ros-${ROS_DISTRO}-compressed-image-transport \
    ros-${ROS_DISTRO}-image-publisher \
    ros-${ROS_DISTRO}-image-pipeline \
    ros-${ROS_DISTRO}-image-view \
    ros-${ROS_DISTRO}-camera-info-manager \
    ros-${ROS_DISTRO}-camera-calibration-parsers \
    ros-${ROS_DISTRO}-camera-calibration \
    ros-${ROS_DISTRO}-v4l2-camera \
    ros-${ROS_DISTRO}-apriltag-ros \
    ros-${ROS_DISTRO}-pcl-conversions \
    ros-${ROS_DISTRO}-pcl-ros \
    ros-${ROS_DISTRO}-pcl-msgs

# Control
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-ros2-control \
    ros-${ROS_DISTRO}-ros2-controllers \
    ros-${ROS_DISTRO}-controller-manager \
    ros-${ROS_DISTRO}-ros2controlcli \
    ros-${ROS_DISTRO}-transmission-interface \
    ros-${ROS_DISTRO}-backward-ros \
    ros-${ROS_DISTRO}-ur

# Navigation / SLAM
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-slam-toolbox \
    ros-${ROS_DISTRO}-navigation2 \
    ros-${ROS_DISTRO}-nav2-bringup \
    ros-${ROS_DISTRO}-nav2-msgs \
    ros-${ROS_DISTRO}-nav2-map-server \
    ros-${ROS_DISTRO}-nav2-rviz-plugins \
    ros-${ROS_DISTRO}-robot-localization \
    ros-${ROS_DISTRO}-pointcloud-to-laserscan

# Misc utilities
apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-teleop-twist-keyboard \
    ros-${ROS_DISTRO}-imu-tools \
    ros-${ROS_DISTRO}-topic-tools \
    ros-${ROS_DISTRO}-rosbridge-suite

# Gazebo (heavy; optional for headless / lightweight targets)
if [ "$WITH_GAZEBO" = "1" ]; then
    apt-get install -y --no-install-recommends \
        ros-${ROS_DISTRO}-gazebo-ros \
        ros-${ROS_DISTRO}-gazebo-ros-pkgs \
        ros-${ROS_DISTRO}-gazebo-ros2-control \
        ros-${ROS_DISTRO}-gazebo-plugins
fi

rm -rf /var/lib/apt/lists/*
