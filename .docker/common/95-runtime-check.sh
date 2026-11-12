#!/usr/bin/env bash
# Fail the build if the API/voice runtime or the launcher's DDS is incomplete.
# No hardware, model downloads, API keys or running ROS graph are needed.
set -euo pipefail
: "${ROS_DISTRO:?ROS_DISTRO must be set}"

# ROS setup adds vendor library paths too (e.g. CycloneDDS's libddsc).
set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u
python3 - <<'PY'
import ctypes
import importlib
import importlib.metadata
import os

# Loading the shared library also checks its CycloneDDS/iceoryx dependencies.
ctypes.CDLL(f"/opt/ros/{os.environ['ROS_DISTRO']}/lib/librmw_cyclonedds_cpp.so")
modules = (
    "aiohttp", "rank_bm25", "requests", "yaml", "rapidfuzz", "snowballstemmer",
    "numpy", "scipy", "sklearn", "tqdm", "sounddevice", "soxr", "sherpa_onnx",
    "rclpy",
)
for name in modules:
    importlib.import_module(name)

assert importlib.metadata.version("numpy").split(".")[0] == "1"
assert importlib.metadata.version("pytest") == "8.4.2"
assert int(importlib.metadata.version("setuptools").split(".")[0]) < 80
assert importlib.metadata.version("openwakeword") == "0.6.0"

# WITH_ML=0 intentionally omits inference runtimes on the full Jetson image.
if os.environ.get("WITH_ML", "1") != "0":
    importlib.import_module("onnxruntime")
    importlib.import_module("openwakeword.model")

print("Runtime OK: CycloneDDS, API, search, audio, ASR and wakeword dependencies")
PY
