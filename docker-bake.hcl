# docker buildx bake            -> builds all native-buildable targets
# docker buildx bake x86-gpu    -> single target
# docker buildx bake jetson     -> cross-build arm64 (needs QEMU set up)
#
# Override the image name/tag prefix:
#   IMAGE=myrepo/robot docker buildx bake

variable "IMAGE" {
  default = "fabook/iros"
}

variable "USERNAME" {
  default = "fabian"
}

# Default group: the two x86 images (safe to build on a normal x86 host).
# Jetson targets are arm64 and are built explicitly (on-device or with QEMU).
group "default" {
  targets = ["x86-cpu", "x86-gpu"]
}

group "arm" {
  targets = ["jetson", "jetson-cpu"]
}

group "all" {
  targets = ["x86-cpu", "x86-gpu", "jetson", "jetson-cpu"]
}

target "_common" {
  context    = "."
  args = {
    USERNAME = "${USERNAME}"
  }
}

target "x86-gpu" {
  inherits   = ["_common"]
  dockerfile = ".docker/Dockerfile.x86-gpu"
  tags       = ["${IMAGE}:x86-gpu"]
  platforms  = ["linux/amd64"]
}

target "x86-cpu" {
  inherits   = ["_common"]
  dockerfile = ".docker/Dockerfile.x86-cpu"
  tags       = ["${IMAGE}:x86-cpu"]
  platforms  = ["linux/amd64"]
}

target "jetson" {
  inherits   = ["_common"]
  dockerfile = ".docker/Dockerfile.jetson"
  tags       = ["${IMAGE}:jetson"]
  platforms  = ["linux/arm64"]
}

target "jetson-cpu" {
  inherits   = ["_common"]
  dockerfile = ".docker/Dockerfile.jetson-cpu"
  tags       = ["${IMAGE}:jetson-cpu"]
  platforms  = ["linux/arm64"]
}
