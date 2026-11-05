#!/usr/bin/env python3
"""Validate a z0live.lanes.v1 config (emitted by the z0live lanes card). Stdlib only.

  lanes.py check FILE   -> prints issues, exit 1 if any
  lanes.py write FILE   -> validates, then atomically writes to the broker path
                           ($Z0LIVE_LANES or $Z0INT_HOME/config/z0live-lanes.json)

Council legality is the AODL compiler's: this only checks the card carried an ok compile receipt.
v1.1 keys (optional, still schema z0live.lanes.v1; RESEARCH.md C2/C3/C4 in
data/operations/z0live-lanes-20261105): smol.trigger (partial-transcript heartbeat), smol.router,
driver.start/parallel, top-level blackboard object, splice.gate/gate_kinds/gate_ms.
Design: data/operations/z0live-phone-20261105/LANES.md
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "z0live.lanes.v1"
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")  # broker voice_runtime.IDENT plus '@'
LANES = {"voice": 0, "smol": 1, "driver": 2, "council": 3}
ACTORS = {"voice_plan", "grok_voice", "on_device"}
SMOL_ROUTES = {"vllm-sr", "local", "voice_plan"}
TRIGGERS = {"owner_asks", "driver_escalates"}
LATE = {"drop", "correct_if_changed"}
KINDS = {"simple", "math", "gather", "hard", "chat"}  # router kinds (E1 FAST_P)
CEIL = {"ack_within_ms": 1500, "deadline_ms_smol": 3000, "bridge_after_ms": 5000,
        "deadline_ms_driver": 300000, "deadline_ms_council": 1800000, "late_window_ms": 120000}


def broker_path() -> Path:
    home = Path(os.environ.get("Z0INT_HOME", str(Path.home() / ".z0int")))
    return Path(os.environ.get("Z0LIVE_LANES", str(home / "config" / "z0live-lanes.json")))


def _ms(v, ceil):
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= ceil


def _binding(b, where, out):
    if not isinstance(b, dict) or not all(isinstance(b.get(k), str) and IDENT.fullmatch(b[k]) for k in ("provider", "model", "route")):
        out.append(f"{where}: needs provider/model/route identifiers")
        return False
    p, r = b["provider"], b["route"]
    if p == "openai" and r != "fx":
        out.append(f"{where}: GPT models route only via fx")
    if p == "google" and r != "agy":
        out.append(f"{where}: Gemini routes only via agy")
    if r == "vllm-sr" and p != "free":
        out.append(f"{where}: vllm-sr carries free models only")
    return True


def validate(cfg) -> list[str]:
    out: list[str] = []
    if not isinstance(cfg, dict) or cfg.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    if not isinstance(cfg.get("revision"), str) or not cfg["revision"]:
        out.append("revision required")
    lanes = cfg.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        return out + ["lanes must be exactly voice, smol, driver, council"]
    for name, idx in LANES.items():
        if not isinstance(lanes[name], dict) or lanes[name].get("lane") != idx:
            return out + [f"lanes.{name}.lane must be {idx}"]
    v, s, d, c = (lanes[k] for k in ("voice", "smol", "driver", "council"))
    sp: dict = cfg["splice"] if isinstance(cfg.get("splice"), dict) else {}

    # lane 0
    if v.get("actor") not in ACTORS:
        out.append(f"voice.actor must be one of {sorted(ACTORS)}")
    if v.get("actor") == "on_device":
        for k in ("vad", "asr", "tts"):
            if not isinstance(v.get(k), str) or not IDENT.fullmatch(v[k]):
                out.append(f"voice.{k} required for on_device")
        if str(v.get("tts", "")).lower().startswith("kokoro"):
            out.append("voice.tts: Kokoro is never the default TTS path")
    if not _ms(v.get("ack_within_ms"), CEIL["ack_within_ms"]):
        out.append("voice.ack_within_ms out of range")

    # lane 1
    models = s.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= 6:
        out.append("smol.models must list 1..6 bindings")
        models = []
    for i, b in enumerate(models):
        if _binding(b, f"smol.models[{i}]", out):
            if b["route"] not in SMOL_ROUTES:
                out.append(f"smol.models[{i}]: route must be vllm-sr, local or voice_plan")
            if b["route"] != "voice_plan" and b.get("billing") != "free":
                out.append(f"smol.models[{i}]: non-voice_plan smol lanes must be free")
    if s.get("fanout") != "race":
        out.append("smol.fanout must be race")
    if s.get("tools") is not False:
        out.append("smol.tools must be false (answer|meta only)")
    if not _ms(s.get("deadline_ms"), CEIL["deadline_ms_smol"]):
        out.append("smol.deadline_ms out of range")

    # lane 2
    db = d.get("model")
    if _binding(db, "driver.model", out) and not (db["route"] == "voice_plan" or (db["provider"] == "anthropic" and db["route"] == "hermes")):
        out.append("driver.model: Anthropic via hermes, or voice_plan meta_role")
    if not isinstance(d.get("verify_smol"), bool):
        out.append("driver.verify_smol must be boolean")
    if not _ms(d.get("deadline_ms"), CEIL["deadline_ms_driver"]):
        out.append("driver.deadline_ms out of range")

    # lane 3
    if not isinstance(c.get("enabled"), bool):
        out.append("council.enabled must be boolean")
    if c.get("pattern") != "socratic-council":
        out.append("council.pattern must be socratic-council")
    params = c.get("params") if isinstance(c.get("params"), dict) else {}
    if params.get("approval") != "owner":
        out.append("council.params.approval must be owner")
    when = c.get("invoke", {}).get("when") if isinstance(c.get("invoke"), dict) else None
    if not isinstance(when, list) or not when or not set(when) <= TRIGGERS:
        out.append(f"council.invoke.when must be a non-empty subset of {sorted(TRIGGERS)}")
    mpc = c.get("invoke", {}).get("max_per_call") if isinstance(c.get("invoke"), dict) else None
    if not (isinstance(mpc, int) and not isinstance(mpc, bool) and 0 <= mpc <= 3):
        out.append("council.invoke.max_per_call must be 0..3")
    if not _ms(c.get("deadline_ms"), CEIL["deadline_ms_council"]):
        out.append("council.deadline_ms out of range")
    if c.get("enabled") is True:
        rc = c.get("compile") if isinstance(c.get("compile"), dict) else {}
        if rc.get("ok") is not True or not re.fullmatch(r"[0-9a-f]{12}", str(rc.get("planHash", ""))):
            out.append("council.compile: enabled council needs an ok AODL compile receipt (planHash)")

    # splice
    if sp.get("barge_in") != "cancel_epoch":
        out.append("splice.barge_in must be cancel_epoch (Z0LiveBrokerSession epoch)")
    if sp.get("late_result") not in LATE:
        out.append(f"splice.late_result must be one of {sorted(LATE)}")
    for k, ceil in (("bridge_after_ms", CEIL["bridge_after_ms"]), ("late_window_ms", CEIL["late_window_ms"])):
        if not _ms(sp.get(k), ceil):
            out.append(f"splice.{k} out of range")
    for k in ("max_bridges", "max_corrections"):
        if not (isinstance(sp.get(k), int) and not isinstance(sp.get(k), bool) and 0 <= sp[k] <= 2):
            out.append(f"splice.{k} must be 0..2")

    out += _v11(cfg, s, d, sp)
    if not out:  # latency order only once every field is a valid int
        order = [("voice.ack_within_ms", v["ack_within_ms"]), ("smol.deadline_ms", s["deadline_ms"]),
                 ("splice.bridge_after_ms", sp["bridge_after_ms"])]
        for (a, x), (b, y) in zip(order, order[1:]):
            if x > y:
                out.append(f"latency order: {a} <= {b}")
        if sp["bridge_after_ms"] >= d["deadline_ms"]:
            out.append("latency order: splice.bridge_after_ms < driver.deadline_ms")
        if d["deadline_ms"] > c["deadline_ms"]:
            out.append("latency order: driver.deadline_ms <= council.deadline_ms")
    return out


def _v11(cfg, s, d, sp) -> list[str]:
    """Optional v1.1 keys: absent means v1 behaviour (finals only, verify after smol, no gate, pull)."""
    out: list[str] = []
    trig = s.get("trigger")
    if trig is not None:
        if not isinstance(trig, dict) or trig.get("on") not in {"finals", "partials"}:
            out.append("smol.trigger.on must be finals or partials")
        else:
            if trig.get("on") == "partials" and not _ms(trig.get("heartbeat_ms"), 2000):
                out.append("smol.trigger.heartbeat_ms out of range")
            if not isinstance(trig.get("keep_inflight", True), bool):
                out.append("smol.trigger.keep_inflight must be boolean")
            if trig.get("reuse", "validated") not in {"validated", "never"}:
                out.append("smol.trigger.reuse must be validated or never")
    if "router" in s:
        names = [b.get("model") for b in s.get("models") or [] if isinstance(b, dict)]
        if s["router"] not in names:
            out.append("smol.router must name one of smol.models")
    if d.get("start", "after_smol") not in {"endpoint", "after_smol"}:
        out.append("driver.start must be endpoint or after_smol")
    if "parallel" in d and (not isinstance(d["parallel"], bool) or (d["parallel"] and d.get("start") != "endpoint")):
        out.append("driver.parallel must be boolean and needs driver.start=endpoint")
    bb = cfg.get("blackboard")
    if bb is not None:
        if not isinstance(bb, dict) or bb.get("delivery") not in {"push", "pull"}:
            out.append("blackboard.delivery must be push or pull")
        else:
            r = bb.get("reinject_every_turns", 0)
            if not (isinstance(r, int) and not isinstance(r, bool) and 0 <= r <= 10):
                out.append("blackboard.reinject_every_turns must be 0..10")
            if not isinstance(bb.get("spec_entries", False), bool):
                out.append("blackboard.spec_entries must be boolean")
            cb = bb.get("corrected_by", ["driver", "council"])
            if not isinstance(cb, list) or not cb or not set(cb) <= {"driver", "council"}:
                out.append("blackboard.corrected_by must be a non-empty subset of driver, council")
    gate = sp.get("gate", "none")
    if gate not in {"none", "pre_speak"}:
        out.append("splice.gate must be none or pre_speak")
    elif gate == "pre_speak":
        kinds = sp.get("gate_kinds")
        if not isinstance(kinds, list) or not kinds or not set(kinds) <= KINDS:
            out.append(f"splice.gate_kinds must be a non-empty subset of {sorted(KINDS)}")
        if not _ms(sp.get("gate_ms"), CEIL["bridge_after_ms"]):
            out.append("splice.gate_ms out of range")
        if d.get("verify_smol") is not True:
            out.append("splice.gate=pre_speak needs driver.verify_smol (something must release the gate)")
    return out


def main(argv):
    if len(argv) != 3 or argv[1] not in {"check", "write"}:
        print(__doc__)
        return 2
    cfg = json.loads(Path(argv[2]).read_text())
    issues = validate(cfg)
    for i in issues:
        print("issue:", i)
    if issues:
        return 1
    if argv[1] == "write":
        dest = broker_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        tmp.replace(dest)
        print("wrote", dest)
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
