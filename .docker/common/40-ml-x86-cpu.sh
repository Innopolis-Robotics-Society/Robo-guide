#!/usr/bin/env bash
# ML stack for x86_64 CPU-only (local testing, no GPU).
# torch CPU wheels, plain onnxruntime.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision

python3 -m pip install --no-cache-dir \
    onnxruntime==1.18.1 \
    ultralytics \
    segmentation-models-pytorch
