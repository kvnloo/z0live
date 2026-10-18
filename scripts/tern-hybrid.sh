#!/usr/bin/env bash
# Hybrid: Android mic/browser + z0live/OMP in Tern on that Android phone.
# Does not pair ADB, change Android settings, enable root, grant permissions,
# install models or alter default launcher/assistant.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-check}"
if [[ "$mode" != "check" && "$mode" != "prepare" ]]; then
  echo "Usage: bash scripts/tern-hybrid.sh [check|prepare]" >&2
  exit 2
fi

if ! command -v adb >/dev/null 2>&1; then
  echo "adb unavailable in Tern. Existing authorized Wi-Fi ADB required." >&2
  exit 3
fi

if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  serial="$ANDROID_SERIAL"
else
  mapfile -t devices < <(adb devices | awk 'NR>1 && $2=="device" {print $1}')
  if [[ "${#devices[@]}" -ne 1 ]]; then
    echo "Expected one authorized ADB device. Set ANDROID_SERIAL for multiple." >&2
    exit 4
  fi
  serial="${devices[0]}"
fi

if [[ "$(adb -s "$serial" get-state 2>/dev/null)" != "device" ]]; then
  echo "ADB authorization not ready; keeping the existing pairing untouched." >&2
  exit 5
fi
echo "ADB: one authorized device (no device content captured)"
for cmd in python3 npm; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "$cmd missing in this Tern environment" >&2
    exit 6
  fi
done
if command -v omp >/dev/null 2>&1; then
  echo "OMP: available"
else
  echo "OMP command not on PATH (ensure Tern can start OMP before /brainstorm on)"
fi

if [[ "$mode" == "prepare" ]]; then
  # Only explicit developer-selected preparation; no ADB changes.
  cd "$ROOT"
  python3 -m pip install -e .
  bash scripts/setup-web.sh
  bash scripts/install-omp-extension.sh
  echo "Prepared z0live media UI + OMP extension."
fi

echo "In OMP (Tern): /brainstorm on"
echo "On phone: http://127.0.0.1:8780/voice-setup.html"
echo "Live mic: http://127.0.0.1:8780/"
echo "Keep Hermes phone-harness script approvals enabled. No ADB pairing changed."
