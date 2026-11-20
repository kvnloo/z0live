#!/bin/sh
# Rerun every review-gate repro against the heads it was written for. Read-only for the clone's branches: it adds
# detached worktrees under $TMPDIR and never checks out or edits a branch.
#   usage: review/gate-20261120/run.sh <z0live clone, not the production checkout> <python with aiortc, av, numpy>
# A FAIL is a finding still present at that head; tests named *control* must pass for the finding to count.
set -eu
CLONE=$1
PY=$2
HERE=$(cd "$(dirname "$0")" && pwd)
WT=${TMPDIR:-/tmp}/z0live-gate-wt
for spec in "a70a58a repro_guest_floor.py" "f74e848 repro_guest_floor.py" "2a3b1ff repro_tamil.py" \
            "f74e848 repro_tamil.py" "e692bbf repro_fast_delegation.py"; do
  set -- $spec
  dir=$WT/$1
  [ -d "$dir" ] || git -C "$CLONE" worktree add -q --detach "$dir" "$1"
  echo "== $1 $2"
  (cd "$dir/voice-adapter" && Z0LIVE_DESKTOP=$PWD/../desktop PYTHONWARNINGS=ignore env -u PYTHONPATH "$PY" -m unittest discover \
      -s "$HERE" -p "$2" -v 2>&1 | grep -E '\.\.\. (ok|FAIL|ERROR)$|^Ran |^OK|^FAILED' | sed 's/ (repro_[^)]*)//') || true
done
