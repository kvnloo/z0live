#!/bin/bash
# Install the z0live voice stack as macOS LaunchAgents (Mac Studio). Idempotent: rerun after any change.
#
#   com.zer0.z0live.core   bin/z0live serve on 127.0.0.1:8791 (lanes config: presets, spoken layouts, mid-call edits)
#   com.zer0.z0live.hermes desktop/hermes_host.py, Hermes API-only host on 127.0.0.1:8644 for the core's lanes
#   com.zer0.z0live.voice  voice-adapter/mobile_live.py on 127.0.0.1:9168 (ChatGPT-subscription GPT-Live via codex)
#   com.zer0.z0live.proxy  voice-adapter/dashboard_proxy.py on <tailnet-ip>:9165; voice-live paths -> :9168,
#                          everything else -> the Hermes dashboard on <tailnet-ip>:9119
#
# Secrets never live in this repo or the plists. Optional ~/.config/z0live/voice.env (mode 600, bws-synced)
# is sourced by run.sh, e.g. GROQ_API_KEY for the Groq instant lane.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VA="$REPO/voice-adapter"
TS_IP="${Z0LIVE_TAILNET_IP:-$(/opt/homebrew/bin/tailscale ip -4 | head -1)}"
DATA="$HOME/.local/share/z0live"
LOGS="$HOME/Library/Logs/z0live"
AGENTS="$HOME/Library/LaunchAgents"
PY="${Z0LIVE_PYTHON:-$HOME/.hermes/tools/python-3.14.9+20261021-darwin-arm64/bin/python3}"

mkdir -p "$DATA/voice-traces" "$DATA/call-briefs" "$LOGS" "$AGENTS" "$HOME/.config/z0live"
chmod 700 "$DATA" "$HOME/.config/z0live"

if [ ! -x "$VA/.venv/bin/python" ]; then
  "$PY" -m venv "$VA/.venv"
fi
"$VA/.venv/bin/pip" -q install --only-binary=:all: -r "$VA/requirements.lock"

plist() {  # label, script, env-xml
  cat > "$AGENTS/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>$REPO/deploy/macos/run.sh</string>$(for a in $2; do printf '<string>%s</string>' "$a"; done)</array>
  <key>WorkingDirectory</key><string>$VA</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/$1.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$1.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HERMES_DASHBOARD_UPSTREAM</key><string>$TS_IP:9119</string>
$3
  </dict>
</dict></plist>
EOF
}

plist com.zer0.z0live.voice mobile_live.py "    <key>HERMES_BIN</key><string>$HOME/.hermes/hermes-agent/.hermes/bin/hermes</string>
    <key>HERMES_MOBILE_VOICE_TRACE_DIR</key><string>$DATA/voice-traces</string>
    <key>HERMES_VOICE_BRIEF_DIR</key><string>$DATA/call-briefs</string>
    <key>Z0LIVE_HANDOFF_FILE</key><string>$DATA/handoff.json</string>
    <key>Z0LIVE_DESKTOP</key><string>$REPO/desktop</string>"

plist com.zer0.z0live.proxy dashboard_proxy.py "    <key>HERMES_MOBILE_VOICE_UPSTREAM</key><string>http://127.0.0.1:9168</string>
    <key>HERMES_PROXY_HOSTS</key><string>$TS_IP</string>
    <key>HERMES_PROXY_PORT</key><string>9165</string>"

# z0live core (broker + lanes: presets, spoken layouts, mid-call edits) and its Hermes API-only host.
CORE_ENV="    <key>Z0LIVE_DATA</key><string>$DATA/core</string>
    <key>Z0LIVE_PORT</key><string>8791</string>
    <key>Z0LIVE_LANES_FANOUT</key><string>on</string>
    <key>Z0LIVE_HERMES_BIN</key><string>$HOME/.hermes/hermes-agent/.hermes/bin/hermes</string>
    <key>Z0LIVE_HERMES_SOURCE</key><string>$HOME/.hermes/hermes-agent</string>"
mkdir -p "$DATA/core"; chmod 700 "$DATA/core"
# The voice adapter reaches the core's lanes through a device token (mobile_relay.TOKEN_FILE). Pair once;
# the core keeps only the token's SHA-256, so a lost file means `z0live revoke` + rerun.
RELAY_TOKEN="$HOME/.config/z0live/hermes-mobile-relay.token"
if [ ! -s "$RELAY_TOKEN" ]; then
  ( umask 077; Z0LIVE_DATA="$DATA/core" "$VA/.venv/bin/python" "$REPO/bin/z0live" pair "hermes-mobile-relay-$(hostname -s)" \
      | sed -n 's/^Device token: //p' > "$RELAY_TOKEN" )
fi
plist com.zer0.z0live.core "../bin/z0live serve" "$CORE_ENV"
plist com.zer0.z0live.hermes "../desktop/hermes_host.py" "$CORE_ENV"

for label in com.zer0.z0live.core com.zer0.z0live.hermes com.zer0.z0live.voice com.zer0.z0live.proxy; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  # bootout returns before the old job is gone; bootstrapping then fails with "5: Input/output error".
  for _ in $(seq 40); do launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || break; sleep 0.5; done
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
done
echo "installed; phone URL for voice: http://$TS_IP:9165 (dashboard stays on :9119)"
