#!/usr/bin/env bash
# F4q batch on a GitHub runner (copy of the F4p batch; only the arm flags change): clean, arms A B A2 x 7 reps in
# the F4o rotated order, then the quality gate. A, A2 = --opus-mono (96 kbit/s, complexity unset = 10, A/A);
# B = --opus-mono --opus-complexity 5. Fixtures come from the same espeak-ng `say` shim (bench/ghci/f4o/say on PATH).
# Usage (from voice-adapter/): bench/ghci/f4q/run_batch.sh <out_dir>
set -uo pipefail
OUT="$1"
FX="$OUT/fx"
mkdir -p "$OUT/raw" "$FX"
ORDER=("A B A2" "B A2 A" "A2 A B" "A B A2" "B A2 A" "A2 A B" "A B A2")
LOG="$OUT/batch.txt"
echo "batch start $(date -Iseconds) head=$(git rev-parse --short HEAD)" | tee -a "$LOG"
for rep in 1 2 3 4 5 6 7; do
  for arm in ${ORDER[$((rep - 1))]}; do
    flag=(--opus-mono)
    [ "$arm" = B ] && flag=(--opus-mono --opus-complexity 5)
    name="clean_${arm}_r${rep}"
    echo "$(date -Iseconds) run $name load=$(cut -d' ' -f1-3 /proc/loadavg)" | tee -a "$LOG"
    t0=$(date +%s)
    Z0LIVE_DESKTOP="$PWD/../desktop" env -u PYTHONPATH python -I bench/transport_bench.py --seconds 30 --warmup 3 \
      --seed 4100 --condition clean "${flag[@]}" --fixtures "$FX" --out "$OUT/$name.json" > "$OUT/raw/$name.txt" 2>&1
    rc=$?
    echo "  exit=$rc wall=$(( $(date +%s) - t0 ))s" | tee -a "$LOG"
    [ "$rep" = 1 ] && [ "$arm" = A ] && sha256sum "$FX"/*.wav | sed "s|$FX/||" | tee "$OUT/fixtures.sha256" | tee -a "$LOG"
  done
done
echo "batch done $(date -Iseconds)" | tee -a "$LOG"
env -u PYTHONPATH python -I bench/ghci/f4q/quality.py "$PWD" "$FX" "$OUT/quality.json" > "$OUT/raw/quality.txt" 2>&1
echo "quality exit=$?" | tee -a "$LOG"
env -u PYTHONPATH python -m unittest test_opus_complexity test_opus_bitrate test_opus_mono > "$OUT/raw/unittest.txt" 2>&1
echo "unittest exit=$?" | tee -a "$LOG"
