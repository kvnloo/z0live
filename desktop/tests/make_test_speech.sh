#!/usr/bin/env bash
# Synthesize the fixed acoustic test script with local Piper TTS (no cloud TTS).
# Usage: PIPER=/path/to/piper PIPER_VOICE=/path/en_US-lessac-medium.onnx make_test_speech.sh OUT_DIR
set -euo pipefail
out=${1:?output directory}
mkdir -p "$out"
say() {
  echo "$2" | "${PIPER:?set PIPER}" -m "${PIPER_VOICE:?set PIPER_VOICE}" -f "$out/$1.raw.wav" 2>/dev/null
  # Same level for every utterance: peak-normalized to -3 dBFS, 48 kHz mono 16-bit.
  sox "$out/$1.raw.wav" -r 48000 -b 16 -c 1 "$out/$1.wav" norm -3
  rm -f "$out/$1.raw.wav"
}
say greet "Hi there, how are you doing?"
say knowledge "Quick question. What's the capital of Australia?"
say phone "Open Spotify."
say work "Can you check on my computer which git branch the z0live repo is on right now?"
say barge "Tell me a long story about a lighthouse keeper. Make it at least a minute long."
say interrupt "Wait, stop. What is two plus two?"
ls -1 "$out"
