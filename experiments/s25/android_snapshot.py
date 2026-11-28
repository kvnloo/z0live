#!/usr/bin/env python3
"""Read-only, identity-pinned Samsung S25 CPU/RAM/GPU/thermal snapshot.

No APK, microphone, audio, screen, private app data, permissions changes,
settings, downloads or workload execution. This is NOT a CPU/GPU/NPU benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from rea_protocol import EXPECTED_MODEL, ProbeFailure, confirmed_phone_environment, store_private

# Constant argument vectors only. Do not add an arbitrary shell/exec option.
FACTS = (
    ("model", ("getprop", "ro.product.model")),
    ("soc", ("getprop", "ro.soc.model")),
    ("board", ("getprop", "ro.board.platform")),
    ("memory", ("cat", "/proc/meminfo")),
    ("cpu", ("cat", "/proc/cpuinfo")),
    ("gpu_busy", ("cat", "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage")),
    ("thermal", ("dumpsys", "thermalservice")),
    ("battery", ("dumpsys", "battery")),
)
MAX_FACT_BYTES = 256 * 1024


def pinned_read(wrapper: str, args: tuple[str, ...]) -> tuple[int, bytes, bytes]:
    try:
        result = subprocess.run(
            [wrapper, "exec", "shell", *args],
            capture_output=True, timeout=25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeFailure("pinned read-only ADB command unavailable") from exc
    if len(result.stdout) > MAX_FACT_BYTES or len(result.stderr) > MAX_FACT_BYTES:
        raise ProbeFailure("hardware fact exceeds 256 KiB bounded capture")
    return result.returncode, result.stdout, result.stderr


def memory_mib(raw: bytes) -> dict[str, float]:
    """Extract only /proc/meminfo values we can name. No PSS/GPU/NPU inference."""
    out: dict[str, float] = {}
    for name in ("MemTotal", "MemAvailable"):
        match = re.search(rb"(?m)^" + name.encode() + rb":\s*(\d+)\s+kB\s*$", raw)
        if match:
            out[name + "_MiB"] = round(int(match.group(1)) / 1024, 2)
    return out


def snapshot(*, execute: bool, wrapper: str, output_dir: Path) -> dict:
    receipt: dict = {
        "schema": "z0live.s25.hardware-snapshot.v1",
        "target": EXPECTED_MODEL,
        "status": "NOT_RUN",
        "executed": False,
        "facts": [],
        "memory": {},
        "gpu_inference": "unknown",
        "npu_inference": "unknown",
        "cpu_inference": "unknown",
        "limitations": [
            "Hardware inventory/health is not model inference, throughput or power benchmarking",
            "Driver/NPU presence is not proof of backend execution; require signed, per-model inference receipts",
            "MemAvailable is not allocatable voice-model memory; Android LMKD, PSS, thermals and audio thread priority also constrain admission",
        ],
    }
    if not execute:
        receipt["planned_reads"] = [name for name, _ in FACTS]
        return receipt

    # Strict owner-controlled wrapper validates the saved serial and ADB target first.
    confirmed_phone_environment(wrapper)
    code, model_data, _ = pinned_read(wrapper, FACTS[0][1])
    if code or model_data.decode("utf-8", "replace").strip() != EXPECTED_MODEL:
        raise ProbeFailure("S25 model mismatch/unavailable; stopped before other device reads")

    output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(output_dir, 0o700)
    for idx, (name, args) in enumerate(FACTS):
        if idx == 0:
            rc, raw, stderr = code, model_data, b""
        else:
            rc, raw, stderr = pinned_read(wrapper, args)
        sha = store_private(output_dir / (name + ".txt"), raw)
        status = "PASS" if rc == 0 and bool(raw.strip()) else "UNAVAILABLE"
        receipt["facts"].append({
            "kind": name, "status": status, "sha256": sha,
            "bytes": len(raw), "exit_code": rc,
            "stderr_present": bool(stderr),
        })
        if name == "memory" and status == "PASS":
            receipt["memory"] = memory_mib(raw)
    receipt["executed"] = True
    receipt["status"] = ("PASS" if all(x["status"] == "PASS" for x in receipt["facts"]) else "PARTIAL")
    (output_dir / "summary.json").write_text(json.dumps(receipt, indent=2) + "\n")
    os.chmod(output_dir / "summary.json", 0o600)
    return receipt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true", help="opt into reads on verified phone")
    p.add_argument("--phone-adb", default="phone-adb")
    p.add_argument("--output-dir", type=Path, default=Path.home() / ".local/state/z0live/s25-hardware" / str(int(time.time())))
    a = p.parse_args(argv)
    try:
        result = snapshot(execute=a.execute, wrapper=a.phone_adb, output_dir=a.output_dir)
    except ProbeFailure as exc:
        print(json.dumps({"status":"BLOCKED","error":str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
