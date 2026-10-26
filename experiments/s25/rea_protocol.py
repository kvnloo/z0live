#!/usr/bin/env python3
"""Bounded, read-only REA evidence protocol for the owner's S25 Ultra.

This runner never installs, records audio, starts apps, changes device settings,
or assumes that GPU/NPU services prove usable inference. Full REA evidence
remains local and private; stdout is a minimal digest inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

MAX_REPLY = 1024 * 1024
EXPECTED_MODEL = "SM-S938U"
ALLOWED_EXPORTS = frozenset({"ANDROID_SERIAL", "PHONE_HARNESS_ADB"})
HOST_OPERATIONS = (("doctor",), ("inspect-adb-client",))
PHONE_OPERATIONS = (("inspect-adb-device",), ("list-adb-features",))


class ProbeFailure(Exception):
    pass


def parse_verified_phone_env(output: str) -> dict[str, str]:
    """Accept only restricted 'phone-adb env' exports, never execute a shell."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("export "):
            raise ProbeFailure("unexpected phone-adb environment line")
        try:
            words = shlex.split(line, posix=True)
        except ValueError as exc:
            raise ProbeFailure("invalid phone-adb environment quoting") from exc
        if len(words) != 2 or words[0] != "export" or "=" not in words[1]:
            raise ProbeFailure("invalid phone-adb environment export")
        key, value = words[1].split("=", 1)
        if key in ALLOWED_EXPORTS:
            if not value or key in found:
                raise ProbeFailure("missing or duplicated required phone-adb export")
            found[key] = value
    if set(found) != ALLOWED_EXPORTS:
        raise ProbeFailure("phone-adb did not verify serial and adb client")
    if any(ord(c) < 32 for c in found["ANDROID_SERIAL"]):
        raise ProbeFailure("invalid Android serial")
    return found


def confirmed_phone_environment(wrapper: str) -> dict[str, str]:
    """The existing pinned phone-adb wrapper owns identity verification."""
    if not shutil.which(wrapper):
        raise ProbeFailure("phone-adb wrapper unavailable; no device access attempted")
    try:
        p = subprocess.run([wrapper, "env"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeFailure("phone-adb identity preflight failed") from exc
    if p.returncode:
        raise ProbeFailure("phone-adb identity preflight refused this device")
    return parse_verified_phone_env(p.stdout)


def execute_rea(binary: str, args: tuple[str, ...], environment: dict[str, str]) -> tuple[int, bytes, bytes]:
    try:
        p = subprocess.run(
            [binary, *args, "--json"],
            env=environment,
            capture_output=True,
            timeout=75,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProbeFailure(f"REA operation {args[0]} unavailable or timed out") from exc
    if len(p.stdout) > MAX_REPLY or len(p.stderr) > MAX_REPLY:
        raise ProbeFailure(f"REA operation {args[0]} exceeded 1 MiB output limit")
    return p.returncode, p.stdout, p.stderr


def store_private(path: Path, data: bytes) -> str:
    """No stdout copies of raw evidence, serial, device properties or paths."""
    with path.open("xb") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(data)
    return hashlib.sha256(data).hexdigest()


def safe_operation_name(args: tuple[str, ...]) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", args[0].lower())


def run_protocol(
    *,
    execute: bool,
    phone: bool,
    model_file: Path | None,
    rea_bin: str,
    phone_adb: str,
    output_directory: Path,
) -> dict:
    operations = list(HOST_OPERATIONS)
    if phone:
        operations.extend(PHONE_OPERATIONS)
    if model_file is not None:
        operations.append(("inspect-model-artifact", str(model_file), "--hash", "full"))
    report: dict = {
        "schema": "z0live.s25.rea-protocol.v1",
        "target": EXPECTED_MODEL,
        "executed": False,
        "phone_accessed": False,
        "evidence": [],
        "status": "NOT_RUN",
        "limitations": [
            "REA metadata and Android feature declarations do not prove CPU/GPU/NPU inference",
            "No speech, energy, temperature, or real-audio latency is measured by this protocol",
        ],
    }
    if not execute:
        report["planned_operations"] = [op[0] for op in operations]
        report["limitations"].append("dry run; no REA binary or phone queried")
        return report
    if not shutil.which(rea_bin):
        report["status"] = "CAPABILITY_UNAVAILABLE"
        report["limitations"].append("REA CLI not installed; no protocol executed")
        return report
    if model_file is not None and (not model_file.is_file() or model_file.is_symlink()):
        raise ProbeFailure("model file must be an existing regular non-symlink file")
    device_env = {}
    serial = ""
    if phone:
        device_env = confirmed_phone_environment(phone_adb)
        serial = device_env["ANDROID_SERIAL"]
        report["phone_accessed"] = True
    env = dict(os.environ)
    if device_env:
        env["REA_ADB_PATH"] = device_env["PHONE_HARNESS_ADB"]
        env["ANDROID_SERIAL"] = serial
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output_directory, 0o700)
    for item in operations:
        op = item if item[0] not in {c[0] for c in PHONE_OPERATIONS} else (item[0], serial)
        code, raw, stderr = execute_rea(rea_bin, op, env)
        sha = store_private(output_directory / f"{safe_operation_name(op)}.json", raw)
        valid_json = False
        upstream_ok = False
        try:
            parsed = json.loads(raw)
            valid_json = isinstance(parsed, dict)
            upstream_ok = valid_json and parsed.get("ok") is not False
        except (ValueError, UnicodeError):
            pass
        report["evidence"].append({
            "operation": op[0],
            "sha256": sha,
            "bytes": len(raw),
            "status": "PASS" if code == 0 and upstream_ok else "FAIL",
            "exit_code": code,
            "valid_json": valid_json,
            "stderr_present": bool(stderr),
        })
        if code != 0 or not upstream_ok:
            # Don't inspect later operations on a partially qualified environment.
            break
    report["executed"] = bool(report["evidence"])
    report["status"] = (
        "PASS" if len(report["evidence"]) == len(operations)
        and all(r["status"] == "PASS" for r in report["evidence"]) else "FAIL"
    )
    report["limitations"].append("Evidence is retained locally with 0600 permissions; raw personal device data must never be committed")
    (output_directory / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    os.chmod(output_directory / "summary.json", 0o600)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="run REA; default is dry-run")
    ap.add_argument("--phone", action="store_true", help="read target S25 via verified phone-adb env")
    ap.add_argument("--model-file", type=Path, help="local file to inspect (no download)")
    ap.add_argument("--rea-bin", default="rea")
    ap.add_argument("--phone-adb", default="phone-adb")
    ap.add_argument("--output-dir", type=Path, default=Path.home() / ".local/state/z0live/s25-rea" / str(int(time.time())))
    ns = ap.parse_args(argv)
    try:
        report = run_protocol(execute=ns.execute, phone=ns.phone,
                              model_file=ns.model_file, rea_bin=ns.rea_bin,
                              phone_adb=ns.phone_adb, output_directory=ns.output_dir)
    except ProbeFailure as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "phone_result": "NOT_VERIFIED"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"PASS", "NOT_RUN"} else 1


if __name__ == "__main__":
    sys.exit(main())
