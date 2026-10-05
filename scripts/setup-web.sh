#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/web"

npm install --no-audit --no-fund
npm run build

echo "z0live media client built at $ROOT/web/dist"
