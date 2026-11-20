#!/bin/bash
# Restart the voice adapter on the deployed code as soon as no call is open, then verify it is listening.
# A call counts as open while the newest voice trace has no call.closed event and was written in the last 90 s.
# Usage: deploy-when-idle.sh [--install] [voices.json content to install right before the restart]
#   --install  run install.sh (re-registers plists: needed when LaunchAgent environment or slots change)
set -uo pipefail
ORIG=("$@")
INSTALL=0; [ "${1:-}" = "--install" ] && { INSTALL=1; shift; }
TRACES=${HERMES_MOBILE_VOICE_TRACE_DIR:-$HOME/.local/share/z0live/voice-traces}
VOICES=${Z0LIVE_VOICES_FILE:-$HOME/.config/z0live/voices.json}
LOG=$HOME/.local/share/z0live/deploy.log
if [ -f "$HOME/.local/share/z0live/voice-slots/route.json" ]; then
  if [ -n "${1:-}" ]; then
    [ ! -f "$VOICES" ] || cp "$VOICES" "$VOICES.bak-$(date +%Y%m%d-%H%M%S)" || exit 1
    printf '%s\n' "$1" > "$VOICES" || exit 1
    echo "Voices saved for the next call. No restart needed."
    exit 0
  fi
  echo "Use deploy/macos/rolling-restart.sh for managed voice slots." >&2
  exit 1
fi
open_call() {
  local t; t=$(ls -t "$TRACES"/*/*.jsonl 2>/dev/null | head -1)
  [ -n "$t" ] || return 1
  grep -q '"ev": "call.closed"' "$t" && return 1
  [ $(( $(date +%s) - $(stat -f %m "$t") )) -lt 90 ]
}
echo "$(date '+%F %T') waiting for no open call" | tee -a "$LOG"
while open_call; do sleep 5; done
sleep 3; open_call && exec "$0" "${ORIG[@]}"   # a new call started in the gap: wait again
[ -n "${1:-}" ] && { cp "$VOICES" "$VOICES.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null; printf '%s\n' "$1" > "$VOICES"; }
if [ "$INSTALL" = 1 ]; then
  Z0LIVE_PRIMARY_HOST_PROFILE=poteto "$(dirname "$0")/install.sh" >> "$LOG" 2>&1 || echo "$(date '+%F %T') install.sh exited $?" | tee -a "$LOG"
else
  launchctl kickstart -k "gui/$(id -u)/com.zer0.z0live.voice"
fi
for i in $(seq 1 30); do lsof -nP -iTCP:9168 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1; done
rev=$(git -C "$(dirname "$0")/../.." rev-parse --short HEAD)
if lsof -nP -iTCP:9168 -sTCP:LISTEN >/dev/null 2>&1; then
  h=$(lsof -nP -iTCP:8644 -sTCP:LISTEN >/dev/null 2>&1 && echo up || echo DOWN)
  echo "$(date '+%F %T') deployed $rev (install=$INSTALL); voice 9168 listening; hermes host 8644 $h; voices=$(tr -d '\n' < "$VOICES")" | tee -a "$LOG"
else
  echo "$(date '+%F %T') FAILED: adapter not listening after restart ($rev)" | tee -a "$LOG"; exit 1
fi
