#!/bin/bash
# Run on groot: llama-bench for lane-candidate GGUFs on every attached Android, all devices in parallel.
# Lane-shaped: -p 1024 (lane prompt + brief is ~1k tokens, so ttft ~ 1024 / prefill tok/s), -n 64 (a spoken answer).
# Per run "model:set". all = CPU+OpenCL+Hexagon NPU; nohex = CPU+OpenCL (NPU libs not pushed);
# generic = the CPU-only generic build, libs beside the binary (it loads backends from its own directory).
# Sets (v3): all = auto devices (Hexagon NPU + OpenCL); gpu = -dev GPUOpenCL (Adreno); generic = CPU-only build.
# v2 lesson: the android build links libggml-hexagon.so, so "not pushing the NPU libs" stops it starting;
# pick the device with -dev instead.
# v1 (phone-bench-20261119-2137) lessons: the S22's v69 NPU has no htp lib in b12527; Hexagon segfaults on
# gemma4-e2b and lfm3 on the S25; the generic build found no CPU backend with its libs in lib/; models must be
# deleted after each run (the S25 ran out of space for gemma4-e4b).
set -u
M=/mnt/zer0models/models/phone-slm-20261118
OUT=${OUT:-/mnt/zer0models/workspace/zer0/data/z0live/phone-bench-$(date +%Y%m%d-%H%M)}
mkdir -p "$OUT"
declare -A RUNS=(
  [SM-S938U]="qwen3-1.7b:gpu gemma4-e2b:gpu lfm3-1.7b:gpu"  # e4b (4.6 GB) > its 4.6 GB free; qwen3 npu in v1
  [SM-S906U1]="qwen3-1.7b:gpu qwen3.5-2b:gpu lfm3-1.7b:gpu gemma4-e2b:gpu gemma4-e4b:gpu"
)
setup() {  # $1 serial, $2 set -> echoes the device dir holding llama-bench and its libs
  local s=$1 set=$2 D=/data/local/tmp/z0bench/$2 b=$M/android/llama-b12527
  adb -s "$s" shell "mkdir -p $D /data/local/tmp/z0bench/models" >/dev/null
  if [ "$set" = generic ]; then
    adb -s "$s" push "$M/android-generic/llama-b12527/"*.so "$M/android-generic/llama-b12527/llama-bench" "$D/" >/dev/null
  else
    for f in "$b"/lib/*.so; do
      adb -s "$s" push "$f" "$D/" >/dev/null
    done
    adb -s "$s" push "$b/bin/llama-bench" "$D/" >/dev/null
  fi
  adb -s "$s" shell chmod 755 "$D/llama-bench"
  echo "$D"
}
cleanup_device() {
  adb -s "$1" shell 'cd /data/local/tmp/z0bench 2>/dev/null || exit 0
    rm -f models/*.gguf
    for set in all nohex gpu generic; do rm -f "$set/llama-bench" "$set/"*.so; done
    rmdir models all nohex gpu generic 2>/dev/null || true
    cd .. && (rmdir z0bench 2>/dev/null || true)'
}
run_device() {
  local s=$1 model prev=""; model=$(adb -s "$s" shell getprop ro.product.model | tr -d '\r')
  cleanup_device "$s"
  declare -A ready=()
  for run in ${RUNS[$model]:-}; do
    local g=${run%%:*} set=${run##*:} D
    [ -n "${ready[$set]:-}" ] || ready[$set]=$(setup "$s" "$set")
    D=${ready[$set]}
    if [ "$g" != "$prev" ]; then
      adb -s "$s" shell rm -f "/data/local/tmp/z0bench/models/*.gguf"
      adb -s "$s" push "$M/$g.gguf" "/data/local/tmp/z0bench/models/$g.gguf" >/dev/null \
        || { echo "$model $g push-failed"; continue; }
      prev=$g
    fi
    adb -s "$s" shell "cd $D && LD_LIBRARY_PATH=$D ADSP_LIBRARY_PATH=$D ./llama-bench -m ../models/$g.gguf $( [ $set = gpu ] && echo -dev GPUOpenCL ) -p 1024 -n 64 -r 3 -t 4 -o json" \
      > "$OUT/$model-$g-$set.json" 2> "$OUT/$model-$g-$set.err"
    python3 - "$OUT/$model-$g-$set.json" "$model" "$g" "$set" <<'EOF'
import json, sys
try:
    rows = json.load(open(sys.argv[1]))
    pp = next(r["avg_ts"] for r in rows if r.get("n_prompt") and not r.get("n_gen"))
    tg = next(r["avg_ts"] for r in rows if r.get("n_gen") and not r.get("n_prompt"))
except Exception:
    print(f"{sys.argv[2]:10} {sys.argv[3]:12} {sys.argv[4]:7} FAILED (see .err)"); raise SystemExit
print(f"{sys.argv[2]:10} {sys.argv[3]:12} {sys.argv[4]:7} prefill {pp:7.1f} tok/s  decode {tg:6.1f} tok/s  ttft~{1024/pp:5.2f}s  {rows[0].get('backends','')}")
EOF
  done
  cleanup_device "$s"
}
for s in $(adb devices | awk 'NR>1 && $2=="device"{print $1}'); do run_device "$s" & done
wait
echo "receipts: $OUT"
