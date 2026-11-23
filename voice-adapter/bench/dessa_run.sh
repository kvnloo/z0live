#!/usr/bin/env bash
# One frozen Dessa-replay measurement: engine(s) in parallel, 4 calls x 60 s each (bench/dessa_replay.py defaults).
# Usage: bench/dessa_run.sh LABEL [engine ...]   -> ~/.local/share/z0live/bench/dessa/LABEL-ENGINE.{json,err} + one
# summary line per engine on stdout. Runs in-process calls: it never touches the running voice service.
set -u
cd "$(dirname "$0")/.."
LABEL=$1; shift; ENGINES=${*:-gpt-live grok}
OUT=$HOME/.local/share/z0live/bench/dessa; mkdir -p "$OUT"
export PATH=$PWD/.venv/bin:$HOME/.hermes/tools/ffmpeg-9.0.1-darwin-arm64:$HOME/.local/bin:$PATH
export HERMES_BIN=${HERMES_BIN:-$HOME/.hermes/hermes-agent/.hermes/bin/hermes} Z0LIVE_GUEST=${Z0LIVE_GUEST:-off}
export Z0LIVE_TIERS=${Z0LIVE_TIERS:-on} HERMES_VOICE_BRIEF_DIR=${TMPDIR:-/tmp}/vb Z0LIVE_DESKTOP=$PWD/../desktop
for e in $ENGINES; do
  if [ "$e" = local ]; then
    (umask 077; env -u PYTHONPATH timeout 900 "${DESSA_PYTHON:-.venv/bin/python}" \
      bench/local_voice_replay.py --clip 60 --out "$OUT/$LABEL-$e" > "$OUT/$LABEL-$e.json" 2> "$OUT/$LABEL-$e.err") &
    local_pid=$!
  else
    HERMES_VOICE_ENGINE=$e HERMES_MOBILE_VOICE_TRACE_DIR=$OUT/$LABEL-$e/traces env -u PYTHONPATH timeout 900 \
      "${DESSA_PYTHON:-.venv/bin/python}" bench/dessa_replay.py --clip 60 --out "$OUT/$LABEL-$e" > "$OUT/$LABEL-$e.json" 2> "$OUT/$LABEL-$e.err" &
  fi
done
status=0
if [ -n "${local_pid:-}" ]; then wait "$local_pid" || status=$?; fi
wait
for e in $ENGINES; do
  /usr/bin/python3 - "$OUT/$LABEL-$e.json" "$e" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(sys.argv[2], "FAILED", type(exc).__name__); raise SystemExit
a = d.get("ai") or {}
first = sum(v["spoke_first"] for v in d["per_call"].values())
# Score D (overnight hill-climb log): distance from Dessa on the same clips; lower is better.
D = (abs(a["median_s"] - 0.43) + max(0, a["over_1s_pct"] - 13.2) / 100 + abs(a["backchannels_per_min"] - 3.5) / 10) if a else None
print(f'{sys.argv[2]:9} D={D if D is None else round(D, 3)} calls={d["calls_done"]} errors={len(d["errors"])} takes={a.get("floor_takes")} '
      f'median={a.get("median_s")} iqr={a.get("iqr_s")} overlap%={a.get("overlap_start_pct")} '
      f'>1s%={a.get("over_1s_pct")} bc/min={a.get("backchannels_per_min")} spoke_first={first}')
EOF
done
exit "$status"
