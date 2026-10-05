#!/usr/bin/env bash
set -euo pipefail

Z0INT_VOICE_PLAN_BIN="${Z0INT_VOICE_PLAN_BIN:-z0int-voice-plan}"
Z0LIVE_BIN="${Z0LIVE_BIN:-z0live}"
OUT_ROOT="${Z0LIVE_DOGFOOD_ROOT:-$HOME/.z0live/dogfood}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${1:-$OUT_ROOT/$STAMP}"
mkdir -p "$OUT"

echo "z0live dogfood artifacts: $OUT"

{
  echo "timestamp_utc=$STAMP"
  echo "uname=$(uname -a)"
  if command -v lscpu >/dev/null 2>&1; then
    lscpu
  fi
} >"$OUT/host.txt"

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi     --query-gpu=index,name,driver_version,memory.total,memory.used,memory.free,utilization.gpu     --format=csv >"$OUT/nvidia-smi-before.csv" || true
  nvidia-smi -q >"$OUT/nvidia-smi-before.txt" || true
else
  echo "nvidia-smi unavailable" >"$OUT/nvidia-smi-before.txt"
fi

set +e
"$Z0LIVE_BIN" replay fixtures/core-v1.json --json >"$OUT/core-replay.json"
CORE_RC=$?
"$Z0LIVE_BIN" replay fixtures/stress-v1.json --json >"$OUT/stress-replay.json"
STRESS_RC=$?
"$Z0LIVE_BIN" speculation-replay fixtures/speculation-v1.json --json >"$OUT/speculation-replay.json"
SPEC_RC=$?
"$Z0LIVE_BIN" smoke >"$OUT/smoke.json"
SMOKE_RC=$?

"$Z0INT_VOICE_PLAN_BIN" brainstorm   --harness omp   --pretty   --output "$OUT/voice-plan.json"   >"$OUT/voice-plan.stdout"   2>"$OUT/voice-plan.stderr"
PLAN_RC=$?

PROBE_RC=99
if [[ $PLAN_RC -eq 0 ]]; then
  "$Z0LIVE_BIN" warm-probe     --plan "$OUT/voice-plan.json"     --settle-seconds "${Z0LIVE_PROBE_SETTLE_SECONDS:-2}"     --output "$OUT/warm-probe.json"     >"$OUT/warm-probe.stdout"     2>"$OUT/warm-probe.stderr"
  PROBE_RC=$?
else
  printf '{"schema":"z0live.warm_probe.v1","ok":false,"skipped":true,"reason":"voice_plan_not_admitted"}\n'     >"$OUT/warm-probe.json"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi     --query-gpu=index,name,driver_version,memory.total,memory.used,memory.free,utilization.gpu     --format=csv >"$OUT/nvidia-smi-after.csv" || true
fi
set -e

python - "$OUT" "$CORE_RC" "$STRESS_RC" "$SPEC_RC" "$SMOKE_RC" "$PLAN_RC" "$PROBE_RC" <<'PY'
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
names = ["core_replay", "stress_replay", "speculation_replay", "smoke", "voice_plan", "warm_probe"]
codes = dict(zip(names, map(int, sys.argv[2:])))

def read_json(name: str):
    path = out / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

receipt = {
    "schema": "z0live.dogfood_receipt.v1",
    "status": "PASS" if all(codes[k] == 0 for k in ("core_replay", "stress_replay", "speculation_replay", "smoke", "voice_plan", "warm_probe")) else "INCOMPLETE",
    "exit_codes": codes,
    "voice_plan": read_json("voice-plan.json"),
    "warm_probe": read_json("warm-probe.json"),
    "replay": {
        "core": read_json("core-replay.json"),
        "stress": read_json("stress-replay.json"),
        "speculation": read_json("speculation-replay.json"),
    },
    "artifact_sha256": {
        p.name: digest(p)
        for p in sorted(out.iterdir())
        if p.is_file() and p.name != "receipt.json"
    },
    "limitations": [
        "This receipt measures the local host only.",
        "Headless warm/handshake success does not measure microphone acoustics or conversational naturalness.",
        "No KEEP/promotion claim is implied without frozen z0evals comparison criteria.",
    ],
}
(out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({
    "status": receipt["status"],
    "output": str(out),
    "exit_codes": codes,
}, indent=2))
PY

if [[ $CORE_RC -ne 0 || $STRESS_RC -ne 0 || $SPEC_RC -ne 0 || $SMOKE_RC -ne 0 || $PLAN_RC -ne 0 || $PROBE_RC -ne 0 ]]; then
  exit 1
fi
