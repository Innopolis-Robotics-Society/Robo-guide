#!/usr/bin/env bash
# Build and install can-utils (slcan, candump, etc.) from source.
# Platform-independent (compiles for the target arch).
set -euxo pipefail

cd /tmp
git clone --depth 1 https://github.com/linux-can/can-utils.git
cd can-utils
make
make install
cd /
rm -rf /tmp/can-utils
