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
# A successful restart may only have finished draining an earlier release.
"$DIR/voice-adapter/.venv/bin/python" -B - "$REPO/voice-adapter" "$DIR" <<'PYTHON'
import fcntl
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from voice_slots import Slots

release = Path(sys.argv[2])
release_target = release.resolve(strict=True)
releases = release.parent.resolve()
home = Path.home()
slots = Slots(release.parent.parent / "voice-slots")

with (slots.directory / "deploy.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if slots.release(slots.read().active).resolve(strict=True) != release_target:
        sys.exit(f"{release} is not active; rerun the release after the previous slot drains")

    for link in sorted((home / ".hermes/profiles").glob("*/plugins/z0live")):
        if not link.is_symlink():
            continue
        old = link.readlink()
        try:
            target = (link.parent / old).resolve()
        except (OSError, RuntimeError):
            continue
        if releases not in target.parents or target == release_target:
            continue
        with tempfile.TemporaryDirectory(prefix=".z0live-link-", dir=link.parent) as temp:
            temporary_link = Path(temp) / "z0live"
            subprocess.run(["ln", "-sfn", release, temporary_link], check=True)
            # macOS mv -h replaces a symlink to a directory without following it.
            subprocess.run(["/bin/mv", "-fh", temporary_link, link], check=True)
        print(f"Repointed {link}: {old} -> {release}")
PYTHON
echo "$(date '+%F %T') released $SHA via rolling restart" | tee -a "$HOME/.local/share/z0live/deploy.log"
