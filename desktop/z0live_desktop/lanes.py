"""Broker side of z0live.lanes.v1: validate, hot reload and resolve lane bindings.

`lanes_v1.py` is the card's validator, copied verbatim (kvnloo/aodl-views proto/z0live-lanes 837a714,
prototypes/patterns-v3/lanes/lanes.py); test_lanes_broker.py fails if the copy drifts. On top of it
the broker accepts three additive fields, still schema z0live.lanes.v1:
- `lanes.smol.models[i].tier`: "fast" (intent tracking from partials) or "small" (quick answerers);
  default "small".
- `lanes.driver.verifiers`: 0..3 extra large lanes, same binding rules as `driver.model`. The large
  lanes are `[driver.model] + verifiers`; they audit the blackboard and may correct it.
- A `voice_plan` binding's `model` may name a z0int role (smol/plan/slow/judge...), so lanes bind to
  roles from the pinned z0int.voice_plan.v1, not to hard-coded models.
Design: data/operations/z0live-phone-20261105/LANES.md.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from . import lanes_v1

LOG = logging.getLogger("z0live.lanes")
TIERS = {"fast", "small"}
ROLES = {"smol", "plan", "slow", "task", "default", "judge"}  # z0int.voice_plan.v1 role names
EXAMPLE = "gimme 2 fast lanes, 2 small, 2 large, one council"

# lanes_intent.parse(EXAMPLE) with the council compile receipt; test_lanes_broker asserts equality.
DEFAULT = {
    "schema": "z0live.lanes.v1",
    "revision": "example-2f-2s-2l-1c",
    "lanes": {
        "voice": {"lane": 0, "actor": "voice_plan", "ack_within_ms": 600},
        "smol": {"lane": 1, "fanout": "race", "tools": False, "deadline_ms": 1200, "models": [
            {"provider": "local", "model": "qwen3-0.6b-q8", "route": "local", "billing": "free", "tier": "fast"},
            {"provider": "local", "model": "qwen3-1.7b", "route": "local", "billing": "free", "tier": "fast"},
            {"provider": "voice_plan", "model": "smol", "route": "voice_plan", "billing": "subscription", "tier": "small"},
            {"provider": "local", "model": "qwen3.5-4b", "route": "local", "billing": "free", "tier": "small"},
        ]},
        "driver": {"lane": 2, "model": {"provider": "voice_plan", "model": "plan", "route": "voice_plan"},
                   "verifiers": [{"provider": "anthropic", "model": "claude-sonnet-5", "route": "hermes"}],
                   "verify_smol": True, "deadline_ms": 90000},
        "council": {"lane": 3, "enabled": True, "pattern": "socratic-council",
                    "params": {"rounds": 3, "seats": ["opus-5.5", "sol", "argon"], "researchPool": {},
                               "maxDelegationDepth": 1, "approval": "owner"},
                    "invoke": {"when": ["owner_asks", "driver_escalates"], "max_per_call": 1},
                    "deadline_ms": 900000, "compile": {"ok": True, "planHash": "99c0f4fe7e0a", "issues": []}},
    },
    "splice": {"barge_in": "cancel_epoch", "bridge_after_ms": 2500, "max_bridges": 1,
               "late_result": "correct_if_changed", "late_window_ms": 30000, "max_corrections": 1},
}


# route "local": the llama-server instances already serving these GGUFs on groot (OpenAI-compatible).
# $Z0LIVE_LOCAL_MODELS (JSON {model: base_url}) overrides; an unknown local model is no answer.
LOCAL = {"qwen3-0.6b-q8": "http://127.0.0.1:15637", "qwen3-1.7b": "http://127.0.0.1:11542",
         "qwen3.5-4b": "http://127.0.0.1:11540"}


def local_base(model):
    try:
        extra = json.loads(os.environ.get("Z0LIVE_LOCAL_MODELS", "{}"))
    except ValueError:
        extra = {}
    return {**LOCAL, **(extra if isinstance(extra, dict) else {})}.get(model)


def _large(b, where, out):
    if not lanes_v1._binding(b, where, out):
        return
    if b["route"] == "voice_plan":
        if b["model"] not in ROLES | {"meta_role"}:
            out.append(f"{where}: voice_plan binding must name a z0int role or meta_role")
    elif not (b["provider"] == "anthropic" and b["route"] == "hermes"):
        out.append(f"{where}: Anthropic via hermes, or a voice_plan role")


def validate(cfg) -> list[str]:
    """lanes_v1.validate (the card's rules), then the broker's additive fields."""
    out = lanes_v1.validate(cfg)
    if out:
        return out
    for i, b in enumerate(cfg["lanes"]["smol"]["models"]):
        if b.get("tier", "small") not in TIERS:
            out.append(f"smol.models[{i}].tier must be fast or small")
        if b["route"] == "voice_plan" and b["model"] not in ROLES:
            out.append(f"smol.models[{i}]: voice_plan binding must name a z0int role")
    driver = cfg["lanes"]["driver"]
    _large(driver["model"], "driver.model", out)
    verifiers = driver.get("verifiers", [])
    if not isinstance(verifiers, list) or len(verifiers) > 3:
        out.append("driver.verifiers must list 0..3 bindings")
    else:
        for i, b in enumerate(verifiers):
            _large(b, f"driver.verifiers[{i}]", out)
    return out


def tier(cfg, name):
    return [b for b in cfg["lanes"]["smol"]["models"] if b.get("tier", "small") == name]


def large(cfg):
    d = cfg["lanes"]["driver"]
    return [d["model"], *d.get("verifiers", [])]


def resolve(binding, plan) -> dict:
    """Concrete {provider, model, route} for one lane. Role bindings read the pinned VoicePlan.

    Raises ValueError instead of running a binding that breaks the route laws (owner rule 11-04:
    GPT only via fx/pi, Gemini only via agy), even when the VoicePlan role points there.
    """
    if binding["route"] != "voice_plan":
        return {"provider": binding["provider"], "model": binding["model"], "route": binding["route"]}
    if not plan:
        raise ValueError("role binding needs a pinned VoicePlan")
    role = plan.get("meta_role") if binding["model"] == "meta_role" else binding["model"]
    bound = (plan.get("roles") or {}).get(role)
    if not isinstance(bound, dict) or not bound.get("provider") or not bound.get("model"):
        raise ValueError(f"VoicePlan has no role {role}")
    provider, model = str(bound["provider"]), str(bound["model"])
    if bound.get("harness", "hermes") != "hermes":
        raise ValueError(f"role {role} runs on harness {bound['harness']}, not as a lane")
    if provider.startswith("openai") or model.startswith("gpt-"):
        raise ValueError(f"role {role} is GPT ({model}); GPT runs only via fx/pi, never in a lane")
    if provider.startswith("google") or model.startswith("gemini"):
        raise ValueError(f"role {role} is Gemini ({model}); Gemini runs only via agy")
    return {"provider": provider, "model": model, "route": "hermes", "role": role}


class Lanes:
    """Hot-reloaded config: re-read when the file's mtime/size changes; a bad edit keeps the last good."""

    def __init__(self, path=None):
        self._path = Path(path) if path else None
        self.lock = threading.Lock()
        self.stamp = ("unread",)
        self.cfg, self.source, self.issues = DEFAULT, "default", []

    @property
    def path(self) -> Path:
        return self._path or lanes_v1.broker_path()

    def get(self) -> dict:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        with self.lock:
            if stamp == self.stamp:
                return self.cfg
            self.stamp = stamp
            if stamp is None:
                self.cfg, self.source, self.issues = DEFAULT, "default", []
                return self.cfg
            try:
                cfg = json.loads(self.path.read_text())
                issues = validate(cfg)
            except (OSError, ValueError) as exc:
                issues = [f"unreadable: {type(exc).__name__}"]
            if issues:
                self.issues = issues
                LOG.warning("lanes config %s rejected, keeping %s: %s", self.path, self.source, issues)
            else:
                self.cfg, self.source, self.issues = cfg, str(self.path), []
                LOG.info("lanes config loaded revision=%s from %s", cfg["revision"], self.path)
            return self.cfg

    def status(self) -> dict:
        cfg = self.get()
        return {"revision": cfg["revision"], "source": self.source, "issues": self.issues,
                "fast": len(tier(cfg, "fast")), "small": len(tier(cfg, "small")),
                "large": len(large(cfg)), "council": cfg["lanes"]["council"]["enabled"]}


def write(cfg, path=None) -> Path:
    """Validate and atomically replace the broker config (hot reload picks it up)."""
    issues = validate(cfg)
    if issues:
        raise ValueError("; ".join(issues))
    dest = Path(path) if path else lanes_v1.broker_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    tmp.replace(dest)
    return dest
