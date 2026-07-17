#!/usr/bin/env bash
# Platform-independent Python packages.
# Deliberately EXCLUDES torch/torchvision/onnxruntime — those are
# platform-specific (CUDA vs CPU vs Jetson wheels) and installed per-target.
set -euxo pipefail

python3 -m pip install --no-cache-dir -U pip setuptools wheel

# protobuf pinned so onnxruntime can resolve later
python3 -m pip install --no-cache-dir --index-url https://pypi.org/simple \
    "protobuf>=4.21.12,<6"

# Common libs. numpy pinned <2 for open3d/onnxruntime compatibility.
python3 -m pip install --no-cache-dir \
    "numpy<2" \
    opencv-python \
    open3d \
    matplotlib \
    pyyaml \
    rapidfuzz \
    sounddevice \
    pyaudio \
    vosk
