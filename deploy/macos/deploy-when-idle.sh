#!/bin/bash
# Restart the voice adapter on the deployed code as soon as no call is open, then verify it is listening.
# A call counts as open while the newest voice trace has no call.closed event and was written in the last 90 s.
# Usage: deploy-when-idle.sh [voices.json content to install right before the restart]
set -uo pipefail
TRACES=${HERMES_MOBILE_VOICE_TRACE_DIR:-$HOME/.local/share/z0live/voice-traces}
VOICES=${Z0LIVE_VOICES_FILE:-$HOME/.config/z0live/voices.json}
LOG=$HOME/.local/share/z0live/deploy.log
open_call() {
  local t; t=$(ls -t "$TRACES"/*/*.jsonl 2>/dev/null | head -1)
  [ -n "$t" ] || return 1
  grep -q '"ev": "call.closed"' "$t" && return 1
  [ $(( $(date +%s) - $(stat -f %m "$t") )) -lt 90 ]
}
echo "$(date '+%F %T') waiting for no open call" | tee -a "$LOG"
while open_call; do sleep 5; done
sleep 3; open_call && exec "$0" "$@"   # a new call started in the gap: wait again
[ -n "${1:-}" ] && { cp "$VOICES" "$VOICES.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null; printf '%s\n' "$1" > "$VOICES"; }
launchctl kickstart -k "gui/$(id -u)/com.zer0.z0live.voice"
for i in $(seq 1 30); do lsof -nP -iTCP:9168 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1; done
rev=$(git -C "$(dirname "$0")/../.." rev-parse --short HEAD)
if lsof -nP -iTCP:9168 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "$(date '+%F %T') deployed $rev; voice adapter listening on 9168; voices=$(tr -d '\n' < "$VOICES")" | tee -a "$LOG"
else
  echo "$(date '+%F %T') FAILED: adapter not listening after restart ($rev)" | tee -a "$LOG"; exit 1
fi
