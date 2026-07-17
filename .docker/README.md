# Docker images

One shared set of build scripts, thin per-platform Dockerfiles on top.

## Layout

```
docker/
  common/               # platform-independent build steps (run in order)
    00-tools.sh         # system tools, X11, audio, build deps
    10-ros2-repo.sh     # ROS2 apt repo (modern keyring) + ros-base + colcon/rosdep
    20-ros2-packages.sh # nav2, slam, image pipeline, control, gazebo (WITH_GAZEBO)
    25-can-utils.sh     # can-utils from source
    30-python-common.sh # opencv, open3d, numpy<2, vosk, ... (NO torch)
    40-ml-x86-gpu.sh    # torch cu124 + onnxruntime-gpu        (x86 GPU only)
    40-ml-x86-cpu.sh    # torch cpu  + onnxruntime             (x86 CPU only)
    40-ml-jetson.sh     # torch L4T wheels + onnxruntime-gpu   (Jetson only)
    40-ml-jetson-cpu.sh # onnxruntime CPU only                 (arm64 CPU only)
    90-user-setup.sh    # user, sudo, rosdep, .bashrc, workspace
  Dockerfile.x86-gpu    # nvidia/cuda base       -> fabook/iros:x86-gpu
  Dockerfile.x86-cpu    # ubuntu:22.04 base      -> fabook/iros:x86-cpu   (lightweight)
  Dockerfile.jetson     # l4t-jetpack base       -> fabook/iros:jetson
  Dockerfile.jetson-cpu # arm64v8/ubuntu base    -> fabook/iros:jetson-cpu (lightweight)
docker-bake.hcl         # one entrypoint for all targets
.dockerignore
```

The numeric prefixes are just execution order. Heavy, rarely-changing layers
(tools, ROS2) come first so Docker's layer cache is reused; the fast-changing
ML layer is near the end.

## Why four images

| Image           | Base                    | Arch  | GPU | Use                                   |
|-----------------|-------------------------|-------|-----|---------------------------------------|
| `x86-gpu`       | `nvidia/cuda:12.9`      | amd64 | yes | main dev/deploy on a CUDA workstation |
| `x86-cpu`       | `ubuntu:22.04`          | amd64 | no  | fast local tests, CI, no GPU          |
| `jetson`        | `l4t-jetpack:r36.4.0`   | arm64 | yes | on-device deploy (JetPack)            |
| `jetson-cpu`    | `arm64v8/ubuntu:22.04`  | arm64 | no  | arm64 logic/ROS2 tests, no JetPack    |

The base image differs per platform on purpose — CUDA vs plain ubuntu vs
JetPack can't be swapped with a build arg, and on Jetson the CUDA/TensorRT
versions are fixed by the base image and can't be changed later. So the split
lives in the Dockerfiles; everything reusable lives in `common/`.

## Build

Single image (from repo root):

```bash
docker build -f .docker/Dockerfile.x86-gpu -t fabook/iros:x86-gpu .
docker build -f .docker/Dockerfile.x86-cpu -t fabook/iros::x86-cpu .
```

All x86 targets at once via bake:

```bash
docker buildx bake            # x86-cpu + x86-gpu
docker buildx bake x86-gpu    # just one
```

Jetson (arm64) — build on the device, or cross-build with QEMU from x86:

```bash
# on the Jetson itself:
docker build -f .docker/Dockerfile.jetson -t fabook/iros:jetson .

# cross-build from an x86 host (one-time QEMU setup):
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx bake jetson
docker buildx bake jetson-cpu
```

## Run

```bash
docker run --gpus all -it fabook/iros:x86-gpu          # x86 + GPU
docker run -it fabook/iros:x86-cpu                     # x86 CPU
docker run --runtime nvidia -it fabook/iros:jetson     # Jetson + GPU
docker run -it fabook/iros:jetson-cpu                  # arm64 CPU
```

## Per-device knobs (build args)

- `JETSON_PIP_INDEX` — match to your JetPack (jp6/cu126, jp5/cu114, ...).
  Set on `Dockerfile.jetson` build:
  `--build-arg JETSON_PIP_INDEX=https://pypi.jetson-ai-lab.dev/jp5/cu114`
- `WITH_GAZEBO` — `1` to include Gazebo, `0` to skip (default 0 on arm/jetson,
  1 on x86). Smaller image when off.
- `USERNAME` / `USER_UID` / `USER_GID` — container user.

## Adding a dependency

- Needed everywhere -> edit the matching `common/*.sh` (one place, all images).
- GPU/CPU/Jetson-specific -> edit that platform's `40-ml-*.sh`.
- Don't add torch to `30-python-common.sh` — it's platform-specific on purpose.
