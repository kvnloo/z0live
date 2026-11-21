#!/bin/bash
# Usage: rolling-restart.sh [prepared-release-directory]
# Omit the release directory to finish a cutover that is still draining.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
exec "$REPO/voice-adapter/.venv/bin/python" "$REPO/voice-adapter/rolling_restart.py" \
  roll "$HOME/.local/share/z0live/voice-slots" "$@"
