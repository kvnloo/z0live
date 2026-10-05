#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${OMP_EXTENSIONS_DIR:-$HOME/.omp/agent/extensions}"
TARGET="$TARGET_DIR/z0live.ts"

mkdir -p "$TARGET_DIR"
ln -sfn "$ROOT/integrations/omp/z0live.ts" "$TARGET"

echo "installed OMP z0live extension:"
echo "  $TARGET -> $ROOT/integrations/omp/z0live.ts"
echo
echo "In OMP:"
echo "  /brainstorm on"
