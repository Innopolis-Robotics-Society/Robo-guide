#!/usr/bin/env bash
# Create the non-root user, set up sudo, rosdep, and .bashrc sourcing.
# Platform-independent. Expects USERNAME / USER_UID / USER_GID in env.
set -euxo pipefail

: "${ROS_DISTRO:?ROS_DISTRO must be set}"
USERNAME="${USERNAME:-fabian}"
USER_UID="${USER_UID:-1000}"
USER_GID="${USER_GID:-$USER_UID}"

export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get install -y --no-install-recommends sudo
rm -rf /var/lib/apt/lists/*

# Create group/user only if they don't already exist (some base images
# ship a uid 1000 user, e.g. ubuntu on newer releases).
if ! getent group "$USER_GID" >/dev/null; then
    groupadd --gid "$USER_GID" "$USERNAME"
fi
if ! id -u "$USERNAME" >/dev/null 2>&1; then
    useradd --uid "$USER_UID" --gid "$USER_GID" -m "$USERNAME"
fi

echo "$USERNAME ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME
chmod 0440 /etc/sudoers.d/$USERNAME

# rosdep init (ignore if already initialized)
rosdep init || true
rosdep update || true

# Source ROS in the user's bashrc
echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /home/$USERNAME/.bashrc
# Source the sensor packages workspace if it was built (see 35-sensor-tools.sh)
echo "[ -f /opt/ros/sensors/setup.bash ] && source /opt/ros/sensors/setup.bash" >> /home/$USERNAME/.bashrc

# Prepare workspace
mkdir -p /home/$USERNAME/ros2_ws/src
chown -R "$USER_UID:$USER_GID" /home/$USERNAME
