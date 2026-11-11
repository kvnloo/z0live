"""Broker side of z0live.lanes.v1: validate, hot reload and resolve lane bindings.

`lanes_v1.py` is the card's validator, copied verbatim (kvnloo/aodl-views proto/z0live-lanes-v1-1-20261105
af65518, prototypes/patterns-v3/lanes/lanes.py; it also checks the v1.1 keys); test_lanes_broker.py fails if the copy drifts. On top of it
the broker accepts three additive fields, still schema z0live.lanes.v1:
- `lanes.smol.models[i].tier`: "fast" (intent tracking from partials) or "small" (quick answerers);
  default "small".
- `lanes.driver.verifiers`: 0..3 extra large lanes, same binding rules as `driver.model`. The large
  lanes are `[driver.model] + verifiers`; they audit the blackboard and may correct it.
- A `voice_plan` binding's `model` may name a z0int role (smol/plan/slow/judge...), so lanes bind to
  roles from the pinned z0int.voice_plan.v1, not to hard-coded models.
Presets (Kevin 11-05: roles are defaults, chosen at launch): D is E1's winner and the default, B is
his 2/2/2/1 example, A is today's voice + 1 smol. `preset(name)` returns a copy; a call picks one (or a
spoken topology) at start and may change it mid-call (lane_fanout.Fanout.launch/configure).
Privacy classes are hard filters on every binding (`allowed`): public -> any, personal -> Claude or
local, location/credentials -> local only.
Design: data/operations/z0live-phone-20261105/LANES.md.
"""
from __future__ import annotations

import json
import logging
import os
import copy
import re
import threading
from pathlib import Path

from . import lanes_v1

LOG = logging.getLogger("z0live.lanes")
TIERS = {"fast", "small"}
ROLES = {"smol", "plan", "slow", "task", "default", "judge"}  # z0int.voice_plan.v1 role names
EXAMPLE = "gimme 2 fast lanes, 2 small, 2 large, one council"

# B: lanes_intent.parse(EXAMPLE, B) with the council compile receipt; test_lanes_broker asserts equality.
B = {
    "schema": "z0live.lanes.v1",
    "revision": "preset-B-2f-2s-2l-1c",
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

# D: E1 round-1 winner (EVAL.md "What to test next" #7 = RESEARCH C2+C3): 0.6b tracks the request on
# partials (heartbeat speculative start, prefix-validated reuse), 1.7b is the kind router, one smol
# answers, Opus starts at the endpoint in parallel, risky kinds are held until Opus has checked them.
D = copy.deepcopy(B)
D["revision"] = "preset-D-2f-1s-1l-0c"
D["lanes"]["smol"]["models"] = [B["lanes"]["smol"]["models"][i] for i in (0, 1, 2)]
D["lanes"]["smol"].update(router="qwen3-1.7b", trigger={"on": "partials", "heartbeat_ms": 400,
                                                        "keep_inflight": True, "reuse": "validated"})
D["lanes"]["driver"] = {"lane": 2, "model": {"provider": "voice_plan", "model": "slow", "route": "voice_plan"},
                        "verifiers": [], "verify_smol": True, "start": "endpoint", "parallel": True,
                        "deadline_ms": 90000}
D["lanes"]["council"].update(enabled=False, invoke={"when": ["owner_asks"], "max_per_call": 0})
D["blackboard"] = {"delivery": "pull", "reinject_every_turns": 0, "spec_entries": True,
                   "corrected_by": ["driver", "council"]}
D["splice"].update(gate="pre_speak", gate_kinds=["math", "hard", "gather"], gate_ms=800)
# L2.1 (11-11 blind judge on 48 real call turns, evals/replay_real.py): the smol lane labels its own kind and
# confidence (no router call), only `chat` speaks unchecked (simple facts were stale), and the tool-using gather
# lane writes the board for the driver but speaks only for engines listed in `speak` (none passed yet).
L = copy.deepcopy(D)
L["revision"] = "preset-L-l21-chat-gather-board"
L["lanes"]["smol"].update(self_label=True, gather={
    "enabled": True, "profile": "clean", "toolsets": "web,session_search,z0_file_search", "max_turns": 6,
    "deadline_ms": 45000, "speak": [], "models": [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "route": "hermes"},
        {"provider": "local", "model": "qwen3.5-4b", "route": "local", "hermes_provider": "custom:local-qwen4b"}]})
L["splice"].update(gate_below=0.7, speak_kinds=["chat"])

# A: today's attached path, voice + one smol, nothing verifies.
A = copy.deepcopy(B)
A["revision"] = "preset-A-0f-1s-1l-0c"
A["lanes"]["smol"]["models"] = [B["lanes"]["smol"]["models"][2]]
A["lanes"]["driver"].update(verifiers=[], verify_smol=False)
A["lanes"]["council"].update(enabled=False, invoke={"when": ["owner_asks"], "max_per_call": 0})

PRESETS = {"L": L, "D": D, "B": B, "A": A}
DEFAULT = L
PRESET_WORDS = {"l": "L", "l2": "L", "default": "L", "winner": "L", "best": "L", "recommended": "L", "d": "D",
                "b": "B", "example": "B", "full": "B", "everything": "B", "all": "B",
                "a": "A", "today": "A", "minimal": "A", "basic": "A", "simple": "A"}


def preset(name) -> dict | None:
    """A copy of preset D/B/A by letter or word ("preset b", "the winner", "minimal"); None if unknown."""
    words = re.findall(r"[a-z]+", str(name).lower())
    words = [w for w in words if w not in {"preset", "use", "the", "start", "live", "with", "topology", "lanes"}]
    key = PRESET_WORDS.get(words[0]) if len(words) == 1 else None
    return copy.deepcopy(PRESETS[key]) if key else None


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
    router = cfg["lanes"]["smol"].get("router")
    if router and router not in [b["model"] for b in tier(cfg, "fast")]:
        out.append("smol.router must be a fast-tier lane")
    driver = cfg["lanes"]["driver"]
    _large(driver["model"], "driver.model", out)
    verifiers = driver.get("verifiers", [])
    if not isinstance(verifiers, list) or len(verifiers) > 3:
        out.append("driver.verifiers must list 0..3 bindings")
    else:
        for i, b in enumerate(verifiers):
            _large(b, f"driver.verifiers[{i}]", out)
    return out


# ------------------------------------------------------------------ privacy (Kevin decision 3, 11-05)
PRIVACY = ("public", "personal", "location", "credentials")  # increasing strictness
_CREDENTIALS = re.compile(r"\b(passwords?|passcodes?|passphrase|api[ -]?keys?|tokens?|secrets?|credentials?|2fa|otp|"
                          r"one[- ]time code|pin( code| number)?|ssh key|private key|card number|credit card|cvv|cvc|"
                          r"ssn|social security|bank account|routing number|seed phrase|log ?in code)\b", re.I)
_LOCATION = re.compile(r"\b(address|where (do )?i live|where i am|where am i|my (house|home|apartment|street|"
                       r"location|neighbou?rhood|city|zip)|zip ?code|postcode|coordinates|gps|latitude|longitude|"
                       r"near me|nearby|directions (home|to my))\b", re.I)
_PERSONAL = re.compile(r"\b(my|mine|me|myself|kevin'?s?|our|i(?:'m|'ve|'d| am| have| had| did| was))\b", re.I)


def privacy_class(text, floor="public") -> str:
    """Rule-based class of what a lane would be sent (transcript + quoted context). Errs strict:
    the class is the strictest match, never lower than `floor` (an explicit caller class)."""
    text = str(text or "")
    found = ("credentials" if _CREDENTIALS.search(text) else "location" if _LOCATION.search(text)
             else "personal" if _PERSONAL.search(text) else "public")
    floor = floor if floor in PRIVACY else "credentials"
    return max(found, floor, key=PRIVACY.index)


def allowed(binding, cls, plan=None) -> bool:
    """Hard filter before dispatch: public -> any lane; personal -> Claude (anthropic) or local;
    location/credentials -> local only. Role bindings are judged by what they resolve to."""
    if cls == "public":
        return True
    try:
        b = resolve(binding, plan)
    except ValueError:
        return False
    local = b["route"] == "local" and b["provider"] == "local"
    if cls == "personal":
        return local or b["provider"] == "anthropic"
    return local


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
