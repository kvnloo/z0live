#!/bin/bash
# One command for the pending voice-stack installs. It waits until no Live Voice call is open, then:
#   1. rolling restart (voice slots): checks it is installed; runs install.sh only if it is not
#   2. Gemma fast lane: starts com.zer0.z0live.gemma (:8650), restarts com.zer0.z0live.lanes so it loads the
#      "local" provider, then opts in by writing docs/examples/tiers-gemma.json to ~/.config/z0live/tiers.json
#      (the old file is backed up). If the Gemma check fails, the old tiers file is restored.
# Usage: deploy/macos/apply-when-idle.sh           wait for idle, then apply
#        deploy/macos/apply-when-idle.sh --check   print open calls; exit 0 when idle, 3 when a call is open
# A call is open when a voice trace has no call.closed event and was written in the last OPEN_S seconds (calls
# write usage events every few seconds), or when a voice slot other than the active one is still draining.
set -uo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
TRACES=${HERMES_MOBILE_VOICE_TRACE_DIR:-$HOME/.local/share/z0live/voice-traces}
ROUTE=${Z0LIVE_ROUTE_FILE:-$HOME/.local/share/z0live/voice-slots/route.json}
TIERS=${Z0LIVE_TIERS_FILE:-$HOME/.config/z0live/tiers.json}
AGENTS=$HOME/Library/LaunchAgents
LOGS=$HOME/Library/Logs/z0live
LOG=$HOME/.local/share/z0live/deploy.log
OPEN_S=${OPEN_S:-120}
D="gui/$(id -u)"
say() { echo "$(date '+%F %T') apply-when-idle: $*" | tee -a "$LOG"; }

open_calls() {  # prints one line per open call
  local now f; now=$(date +%s)
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    grep -q '"ev": "call.closed"' "$f" && continue
    [ $(( now - $(stat -f %m "$f") )) -lt "$OPEN_S" ] && echo "trace $(basename "$f")"
  done < <(find "$TRACES" -name '*.jsonl' -mmin -240 2>/dev/null)
  if [ -f "$ROUTE" ]; then  # a live non-active slot is draining open calls
    local active port; active=$(sed -n 's/.*"active": *\([0-9]*\).*/\1/p' "$ROUTE")
    for port in 9168 9169; do
      [ "$port" = "$active" ] && continue
      lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && echo "slot $port draining"
    done
    grep -q '"phase": *"steady"' "$ROUTE" || echo "rollout in progress ($(tr -d '\n' < "$ROUTE"))"
  fi
}

if [ "${1:-}" = "--check" ]; then
  calls=$(open_calls)
  if [ -n "$calls" ]; then echo "OPEN:"; echo "$calls"; exit 3; fi
  echo "IDLE: no open call"; exit 0
fi

wait_idle() {
  say "waiting for no open call"
  while :; do
    while [ -n "$(open_calls)" ]; do sleep 10; done
    sleep 5; [ -z "$(open_calls)" ] && return   # idle twice in a row: no call started in the gap
  done
}
wait_idle

# 1. Rolling restart.
if [ -f "$ROUTE" ] && [ -f "$AGENTS/com.zer0.z0live.voice-next.plist" ]; then
  active=$(sed -n 's/.*"active": *\([0-9]*\).*/\1/p' "$ROUTE")
  curl -sf -m3 "http://127.0.0.1:$active/_z0live/ready" >/dev/null \
    && say "rolling restart already installed (active slot $active ready)" \
    || { say "FAILED: active slot $active not ready; not touching Gemma"; exit 1; }
else
  say "installing rolling restart (install.sh)"
  Z0LIVE_PRIMARY_HOST_PROFILE=poteto "$REPO/deploy/macos/install.sh" >> "$LOG" 2>&1 || { say "FAILED: install.sh exited $?"; exit 1; }
fi

# 2. Gemma model server (a new job; nothing on a call uses it yet).
label=com.zer0.z0live.gemma
if ! launchctl print "$D/$label" >/dev/null 2>&1; then
  cp "$REPO/deploy/macos/$label.plist" "$AGENTS/$label.plist"
  plutil -insert StandardOutPath -string "$LOGS/$label.log" "$AGENTS/$label.plist"
  plutil -insert StandardErrorPath -string "$LOGS/$label.log" "$AGENTS/$label.plist"
  launchctl bootstrap "$D" "$AGENTS/$label.plist" || { say "FAILED: bootstrap $label"; exit 1; }
fi
for _ in $(seq 90); do curl -sf -m2 http://127.0.0.1:8650/v1/models >/dev/null && break; sleep 2; done
curl -sf -m2 http://127.0.0.1:8650/v1/models >/dev/null || { say "FAILED: Gemma not serving on :8650 (see $LOGS/$label.log)"; exit 1; }
say "Gemma serving on :8650"

# 3. Lane server reload (in-flight lane requests would fail, so re-check idle first).
[ -n "$(open_calls)" ] && wait_idle
launchctl kickstart -k "$D/com.zer0.z0live.lanes"
for _ in $(seq 60); do lsof -nP -iTCP:8646 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1; done

# 4. Opt in, then prove the local lane answers through the lane server.
backup=""
[ -f "$TIERS" ] && { backup="$TIERS.bak-$(date +%Y%m%d-%H%M%S)"; cp "$TIERS" "$backup"; }
cp "$REPO/docs/examples/tiers-gemma.json" "$TIERS"
reply=$(curl -s -m30 http://127.0.0.1:8646/v1/ask -H 'Content-Type: application/json' \
  -d '{"provider": "local", "model": "gemma4-e4b", "system": "Answer in one short sentence.", "user": "What is two plus two?", "max_tokens": 40, "timeout": 20}')
if printf '%s' "$reply" | grep -q '"text": *"[^"]'; then
  say "Gemma fast lane on: $reply (tiers backup: ${backup:-none})"
else
  if [ -n "$backup" ]; then cp "$backup" "$TIERS"; else rm -f "$TIERS"; fi
  say "FAILED: local lane check returned '$reply'; tiers.json restored"; exit 1
fi
