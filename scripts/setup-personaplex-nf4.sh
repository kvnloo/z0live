#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
ROOT="${Z0LIVE_PERSONAPLEX_ROOT:-$HOME/.local/share/z0live/personaplex-7b-v1-bnb-4bit}"
REV="${Z0LIVE_PERSONAPLEX_REV:-bde165223cb92cc30fd8279878978838c12d25c5}"

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required after accepting the nvidia/personaplex-7b-v1 license." >&2
  exit 2
fi

mkdir -p "$(dirname "$ROOT")"
if [[ ! -d "$ROOT/.git" ]]; then
  GIT_LFS_SKIP_SMUDGE=1 git clone https://huggingface.co/brianmatzelle/personaplex-7b-v1-bnb-4bit "$ROOT"
fi

git -C "$ROOT" fetch origin "$REV"
git -C "$ROOT" checkout --detach "$REV"

"$PYTHON_BIN" -m pip install -e "$ROOT/moshi" bitsandbytes

cat <<EOF
PersonaPlex NF4 runtime installed from:
  $ROOT
revision:
  $REV

Run Brainstorm Mode from OMP with:
  Z0LIVE_PERSONAPLEX_ROOT="$ROOT" scripts/z0live-brainstorm.sh

The first live launch may download the gated base PersonaPlex assets. The model
is not started by this setup script and consumes no VRAM until Brainstorm Mode.
EOF
