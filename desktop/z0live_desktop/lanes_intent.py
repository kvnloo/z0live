"""Text/voice lane configuration: "gimme 2 fast lanes, 2 small, 2 large, one council" -> z0live.lanes.v1.

Counts rebuild the lane lists from fixed pools; edits ("make the large lanes Opus and Sonnet")
rebind one tier and keep its count. Model words resolve to bindings; z0int role words (smol,
plan, slow, judge...) resolve to `voice_plan` role bindings, so lanes follow the pinned VoicePlan.
The result always passes lanes.validate; an utterance that would break a rule raises ValueError
with the validator's reasons instead of writing a bad config.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from . import lanes

NUM = {"no": 0, "zero": 0, "none": 0, "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2,
       "pair": 2, "three": 3, "four": 4, "five": 5, "six": 6}
TIER_WORDS = {"fast": "fast", "quick": "fast", "intent": "fast", "small": "small", "smol": "small",
              "large": "large", "big": "large", "smart": "large", "slow": "large", "verifier": "large",
              "council": "council", "councils": "council"}

# Fill order when a count grows the tier (free first for fast/small; anthropic via hermes for large).
POOLS = {
    "fast": [{"provider": "local", "model": "qwen3-0.6b-q8", "route": "local", "billing": "free"},
             {"provider": "local", "model": "qwen3-1.7b", "route": "local", "billing": "free"},
             {"provider": "free", "model": "vllm-sr/free", "route": "vllm-sr", "billing": "free"}],
    "small": [{"provider": "voice_plan", "model": "smol", "route": "voice_plan", "billing": "subscription"},
              {"provider": "local", "model": "qwen3.5-4b", "route": "local", "billing": "free"},
              {"provider": "free", "model": "vllm-sr/free", "route": "vllm-sr", "billing": "free"}],
    "large": [{"provider": "voice_plan", "model": "plan", "route": "voice_plan"},
              {"provider": "anthropic", "model": "claude-sonnet-5", "route": "hermes"},
              {"provider": "voice_plan", "model": "slow", "route": "voice_plan"},
              {"provider": "anthropic", "model": "claude-haiku-4-5", "route": "hermes"}],
}
ANTHROPIC = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
LOCAL = {"qwen 0.6b": "qwen3-0.6b-q8", "qwen 1.7b": "qwen3-1.7b", "qwen 4b": "qwen3.5-4b"}
MODEL_RE = re.compile(r"opus|sonnet|haiku|qwen ?[0-9.]+b|vllm|free|smol|plan|slow|judge|default|task")


def model_binding(word, tier):
    w = word.lower().replace("qwen3", "qwen ").replace("  ", " ").strip()
    if w in ANTHROPIC:
        return {"provider": "anthropic", "model": ANTHROPIC[w], "route": "hermes"}
    if w.replace(" ", "") in {k.replace(" ", "") for k in LOCAL}:
        model = next(v for k, v in LOCAL.items() if k.replace(" ", "") == w.replace(" ", ""))
        return {"provider": "local", "model": model, "route": "local", "billing": "free"}
    if w in {"vllm", "free"}:
        return {"provider": "free", "model": "vllm-sr/free", "route": "vllm-sr", "billing": "free"}
    if w in lanes.ROLES:
        return {"provider": "voice_plan", "model": w, "route": "voice_plan",
                **({"billing": "subscription"} if tier != "large" else {})}
    raise ValueError(f"unknown model {word!r}")


def counts(cfg) -> dict:
    return {"fast": len(lanes.tier(cfg, "fast")), "small": len(lanes.tier(cfg, "small")),
            "large": len(lanes.large(cfg)), "council": int(cfg["lanes"]["council"]["enabled"])}


def _number(token) -> int:
    return int(token) if token.isdigit() else NUM[token]


def _set_tier(cfg, tier, bindings):
    s, d = cfg["lanes"]["smol"], cfg["lanes"]["driver"]
    if tier == "large":
        if not bindings:
            raise ValueError("the large lane needs at least one model (lane 2 is the driver)")
        d["model"] = {k: v for k, v in bindings[0].items() if k != "billing"}
        d["verifiers"] = [{k: v for k, v in b.items() if k != "billing"} for b in bindings[1:]]
    else:
        keep = [b for b in s["models"] if b.get("tier", "small") != tier]
        mine = [{**b, "tier": tier} for b in bindings]
        s["models"] = (mine + keep) if tier == "fast" else (keep + mine)


def _bindings(cfg, tier):
    return lanes.tier(cfg, tier) if tier != "large" else lanes.large(cfg)


def parse(utterance, base=None) -> dict:
    """Apply one spoken/typed lane request to `base` (default: lanes.DEFAULT); returns a valid config."""
    cfg = copy.deepcopy(base or lanes.DEFAULT)
    text = " " + re.sub(r"[^a-z0-9. ]+", " ", utterance.lower()) + " "
    changed = False
    # Edits first: "make the large lanes opus and sonnet", "set fast lanes to qwen 0.6b and vllm".
    for m in re.finditer(r"(?:make|set|use|switch)\s+(?:the\s+)?(\w+)\s+lanes?\s+(?:to\s+|use\s+|be\s+)?"
                         r"((?:[\w.]+(?:\s+[\d.]+b)?)(?:\s+(?:and|,)?\s*[\w.]+(?:\s+[\d.]+b)?)*)", text):
        tier = TIER_WORDS.get(m.group(1))
        if tier not in POOLS:
            continue
        words = MODEL_RE.findall(m.group(2))
        if words:
            _set_tier(cfg, tier, [model_binding(w, tier) for w in words])
            changed = True
    # Counts: "2 fast lanes", "two small", "one council", "no council".
    for m in re.finditer(r"\b(\d|no|zero|none|a|an|one|single|two|couple|pair|three|four|five|six)"
                         r"\s+(?:of\s+)?(fast|quick|intent|small|smol|large|big|smart|slow|verifier|councils?)\b", text):
        n, tier = _number(m.group(1)), TIER_WORDS[m.group(2)]
        if tier == "council":
            cfg["lanes"]["council"]["enabled"] = n > 0
            cfg["lanes"]["council"]["invoke"]["max_per_call"] = min(n, 3) if n else 0
        else:
            have = _bindings(cfg, tier)
            pool = [b for b in POOLS[tier] if all(b["model"] != h["model"] for h in have)]
            if n > len(have) + len(pool):
                raise ValueError(f"only {len(have) + len(pool)} {tier} lanes are available")
            _set_tier(cfg, tier, [{k: v for k, v in b.items() if k != "tier"} for b in (have + pool)[:n]])
        changed = True
    if not changed:
        raise ValueError("no lane counts or edits recognised")
    c = counts(cfg)
    cfg["revision"] = "spoken-%df-%ds-%dl-%dc-" % (c["fast"], c["small"], c["large"], c["council"]) + \
        hashlib.sha256(json.dumps(cfg["lanes"], sort_keys=True).encode()).hexdigest()[:8]
    issues = lanes.validate(cfg)
    if issues:
        raise ValueError("; ".join(issues))
    return cfg


def describe(cfg) -> str:
    """One spoken line confirming the active topology."""
    c = counts(cfg)
    large = ", ".join(b["model"] for b in lanes.large(cfg))
    council = "one council" if c["council"] else "no council"
    return f"{c['fast']} fast, {c['small']} small, {c['large']} large ({large}), {council}."


def apply(utterance, path=None) -> dict:
    """Parse against the live config and write it (the broker hot-reloads). Returns status + summary."""
    current = lanes.Lanes(path).get()
    cfg = parse(utterance, current)
    dest = lanes.write(cfg, path)
    return {"ok": True, "revision": cfg["revision"], "path": str(dest), "say": "Lanes set: " + describe(cfg)}


if __name__ == "__main__":
    import sys
    print(json.dumps(apply(" ".join(sys.argv[1:])) if sys.argv[1:] else lanes.DEFAULT, indent=2))
