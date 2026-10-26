#!/usr/bin/env python3
"""Validate one *observed* S25 backend receipt; NEVER select a voice provider.

This is an evidence admissibility check for z0intelligence's existing VoicePlan
selection, not a second scheduler and not a proof of statement authenticity.
"""
import argparse
import json
import re
from pathlib import Path


SHA = re.compile(r"^[0-9a-f]{64}$")
ACCELERATORS = {"gpu-vulkan", "gpu-opencl", "qnn-htp"}
BACKENDS = ACCELERATORS | {"cpu"}
ROLES = {"slm", "stt", "tts", "floor"}


def audit(receipt: dict) -> dict:
    reasons: list[str] = []
    if not isinstance(receipt, dict):
        return {"status": "INVALID", "reasons": ["not_object"]}
    if receipt.get("schema") != "z0live.s25.backend-evidence.v1":
        reasons.append("schema_mismatch")
    d = receipt.get("device")
    d = d if isinstance(d, dict) else {}
    if d.get("model") != "SM-S938U":
        reasons.append("target_mismatch")
    if not isinstance(d.get("soc"), str) or not d.get("soc", "").upper().startswith("SM8750"):
        reasons.append("soc_not_verified")
    if d.get("identity_verified") is not True:
        reasons.append("device_identity_unverified")
    candidate = receipt.get("candidate")
    candidate = candidate if isinstance(candidate, dict) else {}
    if candidate.get("role") not in ROLES:
        reasons.append("unknown_role")
    if not isinstance(candidate.get("model"), str) or not candidate["model"].strip():
        reasons.append("unknown_model")
    if not isinstance(candidate.get("revision"), str) or not candidate["revision"].strip():
        reasons.append("unpinned_revision")
    if not isinstance(candidate.get("artifact_sha256"), str) or not SHA.fullmatch(candidate["artifact_sha256"]):
        reasons.append("artifact_not_hashed")
    runtime = receipt.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    if runtime.get("backend") not in BACKENDS:
        reasons.append("unknown_backend")
    if runtime.get("executed") is not True:
        reasons.append("no_real_execution")
    if runtime.get("output_checked") is not True:
        reasons.append("output_not_checked")
    if runtime.get("fallback_observed") is not False:
        reasons.append("possible_hidden_fallback")
    if runtime.get("backend") in ACCELERATORS:
        if runtime.get("delegate_loaded") is not True:
            reasons.append("delegate_not_loaded")
        if runtime.get("accelerator_executed") is not True:
            reasons.append("accelerator_not_proven")
    if not isinstance(runtime.get("trace_sha256"), str) or not SHA.fullmatch(runtime["trace_sha256"]):
        reasons.append("backend_trace_not_hashed")
    facts = receipt.get("experiment")
    facts = facts if isinstance(facts, dict) else {}
    if facts.get("type") != "physical_s25":
        reasons.append("not_real_phone")
    if not isinstance(facts.get("trials"), int) or isinstance(facts.get("trials"), bool) or facts["trials"] < 1:
        reasons.append("no_trials")
    if facts.get("audio_measured") is True:
        # Audio still requires the separate #32 real-acoustic release gate.
        pass
    reasons = sorted(set(reasons))
    return {
        "schema": "z0live.s25.backend-audit.v1",
        "status": "BACKEND_EXECUTION_OBSERVED" if not reasons else "UNQUALIFIED",
        "reasons": reasons,
        "voice_realtime_qualified": False,
        "provider_selected": False,
        "notes": "Not an attestation signature. Requires independently checked native logs, real audio and #32 before provider promotion.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path, help="locally stored candidate receipt JSON; do not publish private device traces")
    ns = p.parse_args()
    d = json.loads(ns.receipt.read_text())
    print(json.dumps(audit(d), indent=2))


if __name__ == "__main__":
    main()
