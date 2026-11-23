#!/usr/bin/env bash
# Full render in 5 s GPU pieces (exact frame count each, retried), joined, encoded to H.264 (silent).
# Run on the desktop host from the crate dir. Usage: bash render.sh NAME   ->  out/NAME.mp4
# (After glide-hackathon-v3/src/tools/render-pieces.sh.)
set -e
cd "$(dirname "$0")"
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
B=/mnt/zer0models/workspace/zer0/data/operations/hermes-tern-showcase/target/release/z0live-lanes-v1
NAME=$1; D=out/p-$NAME; mkdir -p $D out/tmp; : > $D/list.txt
[ -e out/$NAME.mp4 ] && { echo "refusing: out/$NAME.mp4 exists"; exit 1; }
LEN=34; TOTAL=$((LEN * 60))
for s in $(seq 0 5 $((LEN - 1))); do
  left=$((TOTAL - s * 60)); want=$(( left < 300 ? left : 300 ))
  e=$(awk "BEGIN{print $s + $want/60 + 0.004}")
  f=$D/p$s.mkv
  for try in 1 2 3; do rm -f $f
    systemd-run --user --quiet --collect --wait --pipe --slice=agent.slice -p MemoryMax=6G --working-directory=$PWD \
      -E TMPDIR=$PWD/out/tmp $B render ${s}s..${e}s -o $f --renderer gpu > $D/p$s.log 2>&1 || true
    n=$(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $f 2>/dev/null || echo 0)
    [ "$n" -ge "$want" ] 2>/dev/null && break; echo "p$s try $try got '$n' want $want"; done
  [ "$n" -ge "$want" ] || { echo FAIL p$s; exit 1; }
  echo "file '$PWD/$f'" >> $D/list.txt
done
ffmpeg -v error -y -f concat -safe 0 -i $D/list.txt -map 0:v -c copy $D/pic.mkv
# setpts + passthrough: -r 60 dropped 2 of 2040 frames on concat timestamp jitter (v1).
ffmpeg -v error -y -i $D/pic.mkv -vf setpts=N/60/TB -fps_mode cfr -r 60 -c:v h264_nvenc -preset p5 -cq 18 -pix_fmt yuv420p -color_primaries bt709 \
  -color_trc bt709 -colorspace bt709 -color_range tv -frames:v $TOTAL -movflags +faststart out/$NAME.mp4
echo "frames $(ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 out/$NAME.mp4) want $TOTAL"
