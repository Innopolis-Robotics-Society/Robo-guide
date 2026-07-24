#!/usr/bin/env bash
#
# Script to install FURO serial devices udev rules on the Jetson Orin Nano host.
set -e

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RULES_FILE="${SCRIPT_DIR}/99-furo-devices.rules"

echo "Installing FURO udev rules..."
if [ ! -f "$RULES_FILE" ]; then
    echo "Error: Rules file not found at $RULES_FILE"
    exit 1
fi

# Copy rules to system folder
sudo cp "$RULES_FILE" /etc/udev/rules.d/

# Reload and trigger rules
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "udev rules installed and triggered successfully!"
