#!/usr/bin/env bash
# ML stack for NVIDIA Jetson (arm64 + JetPack/L4T).
#
# IMPORTANT: torch/torchvision on Jetson must come from NVIDIA's L4T-specific
# builds, NOT from pypi (pypi wheels are x86 or CPU-only arm and won't use the
# Jetson GPU). The correct index depends on your JetPack version.
#
# Below targets JetPack 6.x (CUDA 12.6). Adjust JETSON_PIP_INDEX for your device:
#   JetPack 6.0/6.1 (cu126): https://pypi.jetson-ai-lab.dev/jp6/cu126
#   JetPack 5.x   (cu114):   https://pypi.jetson-ai-lab.dev/jp5/cu114
# TensorRT / cuDNN / CUDA themselves already ship inside the l4t-jetpack base
# image and must NOT be reinstalled here.
set -euxo pipefail

JETSON_PIP_INDEX="${JETSON_PIP_INDEX:-https://pypi.jetson-ai-lab.dev/jp6/cu126}"

python3 -m pip install --no-cache-dir \
    --index-url "${JETSON_PIP_INDEX}" \
    torch torchvision onnxruntime-gpu

# ultralytics/smp from pypi (pure-python-ish, fine on arm64)
python3 -m pip install --no-cache-dir \
    ultralytics \
    segmentation-models-pytorch
