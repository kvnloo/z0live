#!/bin/sh
# Restart the voice adapter only when no call is open and no delegated Hermes turn is running.
# Idle = every "negotiated" since the current process started has a matching "closed".
U=hermes-mobile-subscription-voice
END=$(( $(date +%s) + ${MAX_WAIT:-7200} ))
while [ "$(date +%s)" -lt "$END" ]; do
  PID=$(systemctl --user show -p MainPID --value $U)
  SINCE=$(systemctl --user show -p ActiveEnterTimestamp --value $U)
  LOG=$(journalctl --user _PID="$PID" --since "$SINCE" --no-pager -o cat)
  OPEN=$(( $(printf '%s\n' "$LOG" | grep -c 'voice negotiated') - $(printf '%s\n' "$LOG" | grep -c 'voice closed') ))
  if [ "$OPEN" -le 0 ] && ! pgrep -P "$PID" >/dev/null; then
    systemctl --user restart $U && sleep 3 && systemctl --user is-active $U && echo "restarted $(date +%T)"
    exit 0
  fi
  sleep 20
done
echo "gave up: still busy after ${MAX_WAIT:-7200}s"; exit 1
