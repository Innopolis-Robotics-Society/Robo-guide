#!/usr/bin/env bash
# Lightweight CPU-only ML stack for arm64 (Jetson used purely for ROS2/logic
# tests, or building on an arm64 machine without needing the GPU stack).
#
# No torch, no CUDA — just onnxruntime CPU so lightweight inference / imports
# still work. Keeps the image small and fast to build. If a specific node needs
# torch, add torch CPU arm64 wheels explicitly there.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
    onnxruntime==1.18.1
