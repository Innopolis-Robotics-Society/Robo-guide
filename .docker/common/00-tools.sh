#!/usr/bin/env bash
# System tools and libraries. Platform-independent.
# Runs as root during image build.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update

# Core CLI tools
apt-get install -y --no-install-recommends \
    sudo git wget curl gnupg2 lsb-release ca-certificates \
    nano tree net-tools iputils-ping \
    build-essential cmake pkg-config ninja-build

# GUI / X11 / audio runtime libs (rviz, rqt, sound)
apt-get install -y --no-install-recommends \
    libcanberra-gtk-module libcanberra-gtk3-module \
    at-spi2-core x11-apps xauth \
    alsa-utils pulseaudio \
    libgl1 libglib2.0-0 libgl1-mesa-dev \
    libgflags-dev libdw-dev nlohmann-json3-dev \
    libusb-1.0-0-dev

# Python build deps (pyaudio needs portaudio, etc.)
apt-get install -y --no-install-recommends \
    python3-pip python3-dev python3-venv \
    portaudio19-dev libasound2-dev \
    debconf-utils

rm -rf /var/lib/apt/lists/*
