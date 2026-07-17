#!/usr/bin/env bash
# ML stack for x86_64 + NVIDIA CUDA (dGPU).
# torch built against CUDA (cu124 index), onnxruntime-gpu.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cu124 \
    torch torchvision

python3 -m pip install --no-cache-dir \
    onnxruntime-gpu==1.18.1 \
    ultralytics \
    segmentation-models-pytorch
