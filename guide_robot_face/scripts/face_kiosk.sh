#!/bin/bash
# Compatibility entry point. The host service owns the single face browser.
set -euo pipefail
exec /usr/local/bin/guide-kiosk-face "${FACE_PROFILE:-$HOME/.local/share/guide-face}"
