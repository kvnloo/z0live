#!/bin/bash
# Deploy a commit to the voice adapter without dropping open calls.
# Usage: deploy/macos/release.sh [commit]   (default: HEAD of this checkout)
# Builds ~/.local/share/z0live/releases/<sha> (detached worktree + its own venv), then hands it to rolling-restart.sh,
# which moves new calls to the spare slot and lets open calls finish on the old one. Safe while a call is open.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SHA=$(git -C "$REPO" rev-parse --short "${1:-HEAD}")
DIR=$HOME/.local/share/z0live/releases/$SHA
if [ ! -x "$DIR/voice-adapter/.venv/bin/python" ]; then
  [ -d "$DIR" ] || git -C "$REPO" worktree add -q --detach "$DIR" "$SHA"
  PY=$(readlink -f "$REPO/voice-adapter/.venv/bin/python3")
  "$PY" -m venv "$DIR/voice-adapter/.venv"
  "$DIR/voice-adapter/.venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/voice-adapter/requirements.lock"
fi
"$REPO/deploy/macos/rolling-restart.sh" "$DIR"
echo "$(date '+%F %T') released $SHA via rolling restart" | tee -a "$HOME/.local/share/z0live/deploy.log"
