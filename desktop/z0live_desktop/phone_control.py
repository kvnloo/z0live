"""In-app phone control: tiered target choice and speakable results.

The phone does tier 0 itself (`phone_do`: on-phone lexical/synonym match over the live
accessibility tree, sub-millisecond). When it is unsure it returns `status: choose` with
ranked candidates, and the broker escalates:

* tier 1 - a small model on groot's llama-server router (tailnet, ~15-50 ms warm), one
  grammar-constrained letter, confidence from the renormalised letter probabilities;
* tier 2 - Jev's action chooser (~0.5 s), through the hermes-jev plugin's jevkit, only
  with labels that pass Jev's own privacy check.

Each tier acts only above its confidence floor; otherwise the owner is asked to pick.
Nothing here can invent a target: every tier chooses one id the phone already listed.
Consequential taps (send, pay, delete, install, permissions) come back from the phone as
`confirm_required`; the broker remembers the target and acts only on a spoken yes.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

IN_APP = re.compile(
    r"\b(tap|click|press|hit|push|select|toggle|scroll|swipe|menu|button|sidebar|drawer|"
    r"tab|tabs|icon|field|box|type|enter|check(?:box)?|thumbs|copy|paste|attach|"
    r"read (?:it )?(?:aloud|out)|compose|reply|like|expand|collapse)\b",
    re.I,
)
SCREEN_QUERY = re.compile(
    r"(?:"
    r"(?:read|describe|inspect|check|show(?: me)?|look at) "
    r"(?:(?:the|my|current) )?(?:phone )?(?:screen|ui tree|accessibility tree)"
    r"|(?:what(?:'s| is)|tell me what(?:'s| is)) "
    r"(?:on|visible on) (?:(?:the|my) )?(?:phone )?screen"
    r"|(?:what|which) app is (?:open|in (?:the )?foreground)"
    r")\W*",
    re.I,
)
# "open Spotify" / "launch maps" are app launches (open_app), not in-app steps.
APP_LAUNCH = re.compile(r"^\W*(?:please\s+)?(?:can you\s+)?(?:open|launch|start)\s+(?!the\b|my\b|a\b)\w+\W*$", re.I)
YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|do it|go ahead|confirm(?:ed)?|ok(?:ay)?|please do|send it|that's fine)\b", re.I)
NO = re.compile(r"^\W*(?:no|nope|don't|do not|cancel|stop|never ?mind|wait)\b", re.I)
LETTERS = "ABCDEFGHIJKLMNOP"
TIER1_URL = os.environ.get("Z0LIVE_TIER1_URL", "http://100.113.138.100:11530/v1/chat/completions")
TIER1_MODEL = os.environ.get("Z0LIVE_TIER1_MODEL", "qwen3-4b-q4km")
TIER1_FLOOR = float(os.environ.get("Z0LIVE_TIER1_FLOOR", "0.95"))
TIER1_TIMEOUT = float(os.environ.get("Z0LIVE_TIER1_TIMEOUT", "1.5"))
JEV_TIMEOUT = float(os.environ.get("Z0LIVE_JEV_TIMEOUT", "3.0"))
CONFIRM_TTL = 60.0
JEV_MAX_LABEL = 60


def phone_intent(text: str) -> bool:
    """Conservative command grammar, not mentions of phones/buttons in a question."""
    from . import relay, relay_status
    if relay.wants_relay(text) or relay_status.wants_status(text):
        return False
    command = re.sub(r"^\W*(?:(?:okay|ok|yeah)[, ]+)?(?:please\s+)?"
                     r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?", "", text or "", flags=re.I)
    if re.match(r"(?:don't|do not|never|why|what|did|is|has)\b", command, re.I):
        return False
    verb = re.match(r"(open|launch|go to|tap|click|press|hit|push|select|toggle|scroll|swipe|"
                    r"type|enter|copy|paste|expand|collapse)\s+(?:on\s+)?(\S.*)", command, re.I)
    if not verb:
        return False
    target = verb[2]
    if re.search(r"\b(?:ring|double tap|triple tap|repo|session|backend|bridge|pull request)\b", target, re.I):
        return False
    if verb[1].lower() in {"open", "launch", "go to"}:
        return bool(re.search(r"\b(?:app|screen|settings|menu|sidebar|drawer|tab|phone|"
                              r"spotify|chatgpt|chrome|browser|camera|calendar|whatsapp|"
                              r"messages|contacts|youtube|maps|gallery|gmail)\b", target, re.I))
    return bool(re.search(r"\b(?:app|screen|button|menu|sidebar|drawer|tab|icon|field|box|"
                          r"text|keyboard|send|delete|next|back|home|settings|phone|down|up|"
                          r"left|right|it|that|this|allow|cancel|ok|account|chat)\b", target, re.I))


def screen_intent(text: str) -> bool:
    """A UI-tree read must serve an explicit screen request or a phone operation."""
    from . import relay, relay_status
    if relay.wants_relay(text) or relay_status.wants_status(text):
        return False
    request = re.sub(r"^\W*(?:please\s+)?(?:(?:can|could|would|will) you\s+)?(?:please\s+)?",
                     "", text or "", flags=re.I)
    return phone_intent(text) or bool(SCREEN_QUERY.fullmatch(request))


def authorize(store, sid, tool, arguments) -> bool:
    """Use only fresh human transcripts, never a model's rewritten goal, at dispatch."""
    if tool not in {"phone_do", "phone_act"}:
        return True
    import logging
    with store.connect() as db:
        rows = db.execute("SELECT body,ts FROM events WHERE session_id=? ORDER BY id DESC LIMIT 100",
                          (sid,)).fetchall()
    users = []
    for row in rows:
        body = json.loads(row["body"])
        if body.get("type") == "user_transcript" and str(body.get("text") or "").strip():
            # events.ts is receipt time; body.ts is the human utterance's original time.
            # Offline replay must not refresh an old command's execution authority.
            stamp = body.get("ts", row["ts"])
            try:
                if isinstance(stamp, str):
                    from datetime import datetime
                    stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                stamp = float(stamp)
                if not math.isfinite(stamp) or stamp > time.time() + 5:
                    stamp = 0
            except (ValueError, TypeError, OverflowError):
                stamp = 0
            users.append((body["text"], min(row["ts"], stamp)))
            if len(users) == 2:
                break
    allowed = bool(users and time.time() - users[0][1] <= 60 and phone_intent(users[0][0]))
    if (not allowed and tool == "phone_act" and arguments.get("confirm") is True
            and len(users) == 2 and YES.fullmatch(users[0][0])
            and time.time() - users[1][1] <= 60 and phone_intent(users[1][0])):
        allowed = True
    if not allowed:
        logging.getLogger("z0live.phone_control").warning(
            "phone action refused sid=%s tool=%s refused=not_a_phone_request", sid, tool)
    return allowed


def wants_in_app(text: str, tools: list[dict]) -> bool:
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    return "phone_do" in names and phone_intent(text) and bool(IN_APP.search(text or "")) and not APP_LAUNCH.match(text or "")


def candidates_of(result: dict) -> list[dict]:
    """Escalation candidates from a phone `choose` result: [{id, label, role}]."""
    out, seen = [], set()
    for c in (result.get("candidates") or []) + (result.get("nodes") or []):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or c["id"] in seen:
            continue
        label = str(c.get("l") or c.get("label") or "").strip()
        if not label or c.get("static"):
            continue
        seen.add(c["id"])
        words = str(c.get("v") or "").strip()
        if words and words.lower() not in label.lower():
            label = f"{label[:100]} [{words[:40]}]"
        out.append({"id": c["id"], "label": label[:150], "role": str(c.get("r") or c.get("role") or "button")})
        if len(out) >= len(LETTERS):
            break
    return out


def tier1_prompt(goal: str, cands: list[dict]) -> str:
    rows = "\n".join(f"{LETTERS[i]}) {c['label']} ({c['role']})" for i, c in enumerate(cands))
    return (
        "You control a phone for its owner. These controls are on screen:\n" + rows +
        f"\n\nThe owner said: \"{goal}\"\nWhich ONE control should be tapped? Reply with only its letter."
    )


class Tier1:
    """Grammar-constrained one-letter choice on groot's llama-server router."""

    def __init__(self, url=TIER1_URL, model=TIER1_MODEL, timeout=TIER1_TIMEOUT, opener=None):
        self.url, self.model, self.timeout = url, model, timeout
        self.opener = opener or urllib.request.urlopen
        self.warmed = 0.0

    def warm(self):
        """The router unloads idle models; load ours while the phone runs tier 0 (once a minute)."""
        if time.time() - self.warmed < 60:
            return
        self.warmed = time.time()

        def run():
            try:
                Tier1(self.url, self.model, timeout=30, opener=self.opener).choose(
                    "warm up", [{"id": "a", "label": "OK", "role": "button"}, {"id": "b", "label": "Cancel", "role": "button"}])
            except Exception:  # noqa: BLE001 - warming is best effort
                pass

        threading.Thread(target=run, daemon=True, name="tier1-warm").start()

    def choose(self, goal: str, cands: list[dict]) -> dict:
        letters = LETTERS[: len(cands)]
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": tier1_prompt(goal, cands)}],
            "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20,
            "grammar": "root ::= " + " | ".join(f'"{x}"' for x in letters),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.perf_counter()
        request = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with self.opener(request, timeout=self.timeout) as response:
            reply = json.load(response)
        ms = (time.perf_counter() - started) * 1000
        choice = reply["choices"][0]
        picked = (choice.get("message", {}).get("content") or "").strip()[:1]
        tops = ((choice.get("logprobs") or {}).get("content") or [{}])[0].get("top_logprobs") or []
        probs = {}
        for t in tops:
            tok = str(t.get("token", "")).strip()
            if len(tok) == 1 and tok in letters:
                probs[tok] = probs.get(tok, 0.0) + math.exp(t["logprob"])
        total = sum(probs.values())
        confidence = probs.get(picked, 0.0) / total if total else 0.0
        if picked not in letters:
            return {"tier": "groot", "id": None, "confidence": 0.0, "ms": round(ms, 1), "model": self.model}
        return {"tier": "groot", "id": cands[letters.index(picked)]["id"], "confidence": round(confidence, 3),
                "ms": round(ms, 1), "model": self.model}


def load_jevkit():
    """(choose module, privacy module) from the hermes-jev plugin, or (None, None)."""
    for name, mod in list(sys.modules.items()):
        if name.endswith("jevkit.choose") and hasattr(mod, "choose"):
            return mod, sys.modules.get(name[: -len("choose")] + "privacy")
    roots = [os.environ.get("Z0LIVE_JEVKIT", "")]
    home = Path.home() / ".hermes"
    roots += [str(home / "profiles" / "clean" / "plugins" / "hermes-jev" / "jevkit"),
              str(home / "plugins" / "hermes-jev" / "jevkit")]
    for root in roots:
        init = Path(root) / "__init__.py" if root else None
        if not init or not init.is_file():
            continue
        spec = importlib.util.spec_from_file_location("z0live_jevkit", init, submodule_search_locations=[root])
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["z0live_jevkit"] = module
        spec.loader.exec_module(module)
        return importlib.import_module("z0live_jevkit.choose"), importlib.import_module("z0live_jevkit.privacy")
    return None, None


class Tier2:
    """Jev's action chooser. Fails closed (no id) when Jev or its key is unavailable."""

    def __init__(self, timeout=JEV_TIMEOUT, loader=load_jevkit):
        self.timeout, self.loader = timeout, loader

    def choose(self, goal: str, cands: list[dict]) -> dict:
        started = time.perf_counter()
        choose, privacy = self.loader()
        if choose is None:
            return {"tier": "jev", "id": None, "confidence": 0.0, "ms": 0.0, "error": "jevkit_missing"}
        sensitive = getattr(privacy, "is_sensitive", lambda s: False)
        # Controls only: long labels are content (mail subjects, messages) and stay home.
        table = [c for c in cands if len(c["label"]) <= JEV_MAX_LABEL and not sensitive(c["label"])]
        if not table or sensitive(goal):
            return {"tier": "jev", "id": None, "confidence": 0.0, "ms": 0.0, "error": "private"}
        request = {
            "schema": "jev.action_choice_request_v1", "goal": goal[:2000],
            "regions": [{"id": "r" + c["id"], "role": c["role"], "label": c["label"], "interactive": True} for c in table],
            "candidates": [{"id": c["id"], "description": f"Tap '{c['label']}' ({c['role']})"} for c in table]
            + [{"id": "reobserve", "description": "Look at the screen again"},
               {"id": "abstain", "description": "None of these fits; ask the owner"}],
        }
        try:
            reply = choose.choose(request, timeout=self.timeout)
        except ValueError as exc:
            return {"tier": "jev", "id": None, "confidence": 0.0, "ms": 0.0, "error": str(exc)[:80]}
        ms = (time.perf_counter() - started) * 1000
        picked = reply.get("selected_id")
        ok = picked in {c["id"] for c in table}
        return {"tier": "jev", "id": picked if ok else None, "confidence": float(reply.get("confidence") or 0.0),
                "ms": round(ms, 1), "floor": getattr(choose, "MIN_CONFIDENCE", 0.65)}


class Escalator:
    def __init__(self, tier1=None, tier2=None):
        self.tier1 = tier1 if tier1 is not None else Tier1()
        self.tier2 = tier2 if tier2 is not None else Tier2()

    def warm(self):
        getattr(self.tier1, "warm", lambda: None)()

    def decide(self, goal: str, cands: list[dict]) -> tuple[dict | None, list[dict]]:
        """(decision or None, trail of every tier tried)."""
        trail = []
        if len(cands) == 1:
            return {"tier": "only", "id": cands[0]["id"], "confidence": 1.0, "ms": 0.0}, trail
        if not cands:
            return None, trail
        for tier, floor in ((self.tier1, TIER1_FLOOR), (self.tier2, None)):
            try:
                d = tier.choose(goal, cands)
            except Exception as exc:  # noqa: BLE001 - a broken tier escalates, never guesses
                d = {"tier": getattr(tier, "__class__", type(tier)).__name__.lower(), "id": None,
                     "confidence": 0.0, "error": type(exc).__name__}
            trail.append(d)
            need = floor if floor is not None else d.get("floor", 0.65)
            if d.get("id") and d.get("confidence", 0.0) >= need:
                return d, trail
        return None, trail


class Confirmations:
    """A consequential target waiting for the owner's spoken yes, per session."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}

    def hold(self, sid, target):
        with self.lock:
            self.pending[sid] = {**target, "ts": time.time()}

    def take(self, sid):
        with self.lock:
            item = self.pending.pop(sid, None)
        return item if item and time.time() - item["ts"] <= CONFIRM_TTL else None

    def peek(self, sid):
        with self.lock:
            item = self.pending.get(sid)
        return item if item and time.time() - item["ts"] <= CONFIRM_TTL else None


def speak(tool: str, result: dict | None) -> str:
    """One plain spoken sentence for the phone's in-app result (the voice says it as its own)."""
    if result is None:
        return "The phone didn't answer in time, so I can't tell whether that happened."
    data: dict = result["result"] if isinstance(result.get("result"), dict) else result
    status = data.get("status")
    label = ((data.get("target") or {}).get("label") or "").strip()
    if status == "permission_required":
        return "I can't control apps yet: " + str(data.get("instruction") or "z0live needs accessibility turned on.")
    if status == "blocked_app":
        return "That app is on the protected list, so I won't touch it."
    if status == "confirm_required":
        return f"That would tap {label or 'a button'} — should I?"
    if status == "done":
        if data.get("landed"):
            seen = [s for s in (data.get("appeared") or []) if isinstance(s, str)][:2]
            after = f" Now I see {' and '.join(seen)}." if seen else ""
            return f"Tapped {label or 'it'}.{after}"
        return f"I tapped {label or 'it'}, but nothing changed on screen, so it may not have worked."
    if status in {"not_found", "stale"}:
        return "The screen changed before I could tap. Say it again?"
    if status == "choose":
        return "I'm not sure which control you mean."
    return f"That didn't work on the phone ({status or 'unknown error'})."
