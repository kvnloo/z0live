#!/bin/bash
# launchd entry point: load optional secrets, then exec one voice-adapter script with the repo venv.
set -euo pipefail
cd "$(dirname "$0")/../../voice-adapter"
if [ -f "$HOME/.config/z0live/voice.env" ]; then
  set -a; . "$HOME/.config/z0live/voice.env"; set +a
fi
if [ "${1:-}" = voice-slot ]; then
  exec .venv/bin/python rolling_restart.py run "$HOME/.local/share/z0live/voice-slots" "$2"
fi
# Z0LIVE_PY (set per plist) picks the interpreter: the Hermes runtime for lane_server.py, else the repo venv.
exec "${Z0LIVE_PY:-.venv/bin/python}" "$@"  # paths are relative to voice-adapter/
