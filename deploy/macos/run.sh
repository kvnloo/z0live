#!/bin/bash
# launchd entry point: load optional secrets, then exec one voice-adapter script with the repo venv.
set -euo pipefail
cd "$(dirname "$0")/../../voice-adapter"
if [ -f "$HOME/.config/z0live/voice.env" ]; then
  set -a; . "$HOME/.config/z0live/voice.env"; set +a
fi
exec .venv/bin/python "$1"
