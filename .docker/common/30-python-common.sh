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
    vosk \
    rank_bm25 \
    snowballstemmer

# apt's python3-pytest (6.2.5, Ubuntu 22.04) can't load anyio's pytest11
# plugin (needs pytest>=7's _pytest.scope) -- anyio arrives transitively via
# ultralytics -> huggingface_hub -> httpx in the ML layer below, nothing to
# do with this package's own tests, but its plugin autoloads into every
# `python3 -m pytest` invocation in the image and crashes it. Pinned (not
# open >=7) for a reproducible image; pip's copy lands ahead of apt's on
# sys.path, so the apt package -- a real dependency of
# ros-humble-ament-cmake-pytest/python3-colcon-core -- stays installed but
# shadowed, not removed.
python3 -m pip install --no-cache-dir "pytest==8.4.2"
python3 -m pytest --version
