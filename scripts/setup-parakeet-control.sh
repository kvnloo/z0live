#!/usr/bin/env bash
set -euo pipefail

ROOT="${Z0LIVE_PARAKEET_ROOT:-$HOME/.local/share/z0live/parakeet.cpp}"
MODEL_DIR="${Z0LIVE_MODEL_DIR:-$HOME/.local/share/z0live/models}"
BUILD_DIR="${Z0LIVE_PARAKEET_BUILD:-$ROOT/build-z0live}"
PARAKEET_SHA="${Z0LIVE_PARAKEET_SHA:-2de154c622830b62bfcfb92556dbb71bf0263ddd}"
MODEL_NAME="${Z0LIVE_PARAKEET_MODEL_NAME:-realtime_eou_120m-v1-q8_0.gguf}"
MODEL_URL="${Z0LIVE_PARAKEET_MODEL_URL:-https://huggingface.co/mudler/parakeet-cpp-gguf/resolve/main/$MODEL_NAME?download=true}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

mkdir -p "$(dirname "$ROOT")" "$MODEL_DIR"

if [[ ! -d "$ROOT/.git" ]]; then
  git clone --recursive https://github.com/mudler/parakeet.cpp "$ROOT"
fi

git -C "$ROOT" fetch origin "$PARAKEET_SHA"
git -C "$ROOT" checkout --detach "$PARAKEET_SHA"
git -C "$ROOT" submodule update --init --recursive

cmake -S "$ROOT" -B "$BUILD_DIR"   -DCMAKE_BUILD_TYPE=Release   -DPARAKEET_SHARED=ON   -DPARAKEET_BUILD_CLI=OFF   -DPARAKEET_BUILD_SERVER=OFF   -DPARAKEET_WITH_CED=OFF   -DPARAKEET_WITH_VOICEDETECT=OFF

cmake --build "$BUILD_DIR" -j"$JOBS"

MODEL_PATH="$MODEL_DIR/$MODEL_NAME"
if [[ ! -s "$MODEL_PATH" ]]; then
  curl -L --fail --retry 3 --continue-at -     "$MODEL_URL"     -o "$MODEL_PATH"
fi

LIB_PATH=""
for candidate in   "$BUILD_DIR/libparakeet.so"   "$BUILD_DIR/lib/libparakeet.so"   "$BUILD_DIR/libparakeet.dylib"   "$BUILD_DIR/lib/libparakeet.dylib"
do
  if [[ -f "$candidate" ]]; then
    LIB_PATH="$candidate"
    break
  fi
done

if [[ -z "$LIB_PATH" ]]; then
  echo "built parakeet.cpp but could not locate libparakeet shared library under $BUILD_DIR" >&2
  exit 1
fi

cat <<EOF
parakeet.cpp control lane ready

library:
  $LIB_PATH
model:
  $MODEL_PATH

z0intelligence auto-detects these standard z0live paths when they are under:
  ~/.local/share/z0live/parakeet.cpp/build-z0live
  ~/.local/share/z0live/models

For custom locations:
  export Z0LIVE_PARAKEET_LIB="$LIB_PATH"
  export Z0LIVE_PARAKEET_MODEL="$MODEL_PATH"
EOF
