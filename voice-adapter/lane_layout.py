"""The lanes attached to the conversation: one small JSON layout that the voice tiers and the TV conductor both read.

~/.config/z0live/layout.json (z0live.layout.v1) holds model counts per role:

    {"schema": "z0live.layout.v1",
     "voice": {"fast": {"haiku": 3}, "medium": {"sonnet": 1, "sol": 1}, "slow": {"opus": 1, "sol": 1}},
     "research": {"haiku": 3, "sonnet": 2, "sol": 10, "opus": 2, "sr": 0, "fable": 0}}

"voice" (answer) lanes race on every spoken request (tiers.load renders them as {tier: [binding, ...]}). "research" counts are
the conductor's worker pool for the one-shot TV lanes; a bare "add 3 sol lanes" changes those. No file means no change: tiers.load falls back to tiers.json
and the conductor to its own pool, so the first write seeds from what runs now.

Writes are validated (known models only, no 6.1, caps) and atomic: a flock serializes read-modify-write across the
voice slots and Hermes, and each write goes to its own tmp file before os.replace. Stdlib only: the Hermes plugin
loads this file by path, and the conductor reads the JSON itself.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

SCHEMA = "z0live.layout.v1"
FILE = Path(os.environ.get("Z0LIVE_LAYOUT_FILE", str(Path.home() / ".config/z0live/layout.json")))
TIERS_FILE = Path(os.environ.get("Z0LIVE_TIERS_FILE", str(Path.home() / ".config/z0live/tiers.json")))
POOL_FILE = Path.home() / ".config/z0live/lane-pool.json"
CLAUDE = "claude-subscription-directsdk-experimental"
CODEX = "openai-codex"
SR_URL = os.environ.get("Z0LIVE_SR_URL", "http://100.78.215.21:30899/v1")
VOICE_TIERS = ("fast", "medium", "slow")
# name -> voice binding (None: research only) and its home tier (where "add N <name> lanes" goes on the call).
MODELS = {
    "haiku": {"binding": {"provider": CLAUDE, "model": "claude-haiku-5-5"}, "home": "fast", "label": "Haiku 5.5"},
    "sonnet": {"binding": {"provider": CLAUDE, "model": "claude-sonnet-5-5"}, "home": "medium", "label": "Sonnet 5.5"},
    "opus": {"binding": {"provider": CLAUDE, "model": "claude-opus-5-5"}, "home": "slow", "label": "Opus 5.5"},
    "sol": {"binding": {"provider": CODEX, "model": "gpt-6.2-sol"}, "home": "medium", "label": "GPT 6.2 Sol"},
    # vllm-sr on the mbp picks a free upstream per request (groq/nim); lane_server routes it via base_url.
    "sr": {"binding": {"provider": "custom", "model": "vllm-sr/turbo", "base_url": SR_URL}, "home": "fast",
           "label": "semantic router"},
    "fable": {"binding": None, "home": "research", "label": "Haiku + Fable advisor"},
}
# Words Kevin (or the speech-to-text) uses for each model.
ALIASES = {"haiku": "haiku", "haikus": "haiku", "sonnet": "sonnet", "sonnets": "sonnet", "opus": "opus",
           "opuses": "opus", "sol": "sol", "sols": "sol", "soul": "sol", "sole": "sol", "saul": "sol",
           "gpt": "sol", "codex": "sol", "sr": "sr", "router": "sr", "semantic router": "sr", "free": "sr",
           "fable": "fable", "advisor": "fable"}
CAP_TIER = 6        # racing more copies than this per voice tier only burns quota
CAP_RESEARCH = 12   # per model in the TV pool
CAP_POOL = 30       # whole TV pool
# Seeds when neither layout.json nor the older files say otherwise (matches tiers.json and POOL_DEFAULT, 11-27).
VOICE_DEFAULT = {"fast": {"haiku": 3}, "medium": {"sonnet": 1, "sol": 1}, "slow": {"opus": 1, "sol": 1}}
RESEARCH_DEFAULT = {"haiku": 3, "sonnet": 2, "sol": 10, "opus": 2, "sr": 0, "fable": 0}


class LayoutError(ValueError):
    pass


def valid_names() -> str:
    return ", ".join(MODELS)


def _name(word: str) -> str:
    w = " ".join(str(word or "").lower().split())
    if w in MODELS:
        return w
    if w in ALIASES:
        return ALIASES[w]
    if re.search(r"6\.1|terra", w):
        raise LayoutError(f"no 6.1 models; valid models: {valid_names()}")
    raise LayoutError(f"unknown model {word!r}; valid models: {valid_names()}")


def validate(layout) -> dict:
    """A clean copy of `layout`, or LayoutError naming what is wrong."""
    if not isinstance(layout, dict):
        raise LayoutError("layout must be an object")
    voice, research = layout.get("voice"), layout.get("research")
    if not isinstance(voice, dict) or not isinstance(research, dict):
        raise LayoutError("layout needs voice and research maps")
    out = {"schema": SCHEMA, "voice": {}, "research": {}}
    for tier, models in voice.items():
        if tier not in VOICE_TIERS:
            raise LayoutError(f"unknown voice tier {tier!r}; tiers: {', '.join(VOICE_TIERS)}")
        if not isinstance(models, dict):
            raise LayoutError(f"voice.{tier} must map model -> count")
        clean = {}
        for word, n in models.items():
            name = _name(word)
            if MODELS[name]["binding"] is None:
                raise LayoutError(f"{name} is a TV research lane only, not a voice lane")
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise LayoutError(f"voice.{tier}.{name} count must be a whole number >= 0")
            if n:
                clean[name] = clean.get(name, 0) + n
        if sum(clean.values()) > CAP_TIER:
            raise LayoutError(f"at most {CAP_TIER} lanes per voice tier ({tier} would have {sum(clean.values())})")
        out["voice"][tier] = clean
    if not any(out["voice"].values()):
        raise LayoutError("the call needs at least one voice lane")
    for word, n in research.items():
        name = _name(word)
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise LayoutError(f"research.{name} count must be a whole number >= 0")
        if n > CAP_RESEARCH:
            raise LayoutError(f"at most {CAP_RESEARCH} {name} research lanes")
        out["research"][name] = out["research"].get(name, 0) + n
    if sum(out["research"].values()) > CAP_POOL:
        raise LayoutError(f"at most {CAP_POOL} research lanes in all")
    for key in ("updated", "by"):
        if key in layout:
            out[key] = layout[key]
    return out


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _reverse(binding) -> str | None:
    for name, m in MODELS.items():
        b = m["binding"]
        if b and b["provider"] == binding.get("provider") and b["model"] == binding.get("model"):
            return name
    return None


def from_tiers(tiers) -> dict:
    """{tier: [{provider, model}, ...]} -> voice counts; LayoutError on a binding the layout can't name."""
    voice = {}
    for tier, bindings in (tiers or {}).items():
        counts = voice.setdefault(tier, {})
        for b in bindings:
            name = _reverse(b) if isinstance(b, dict) else None
            if not name:
                raise LayoutError(f"unknown lane binding {b!r}; valid models: {valid_names()}")
            counts[name] = counts.get(name, 0) + 1
    return voice


def seed() -> dict:
    """What runs today, as a layout: tiers.json (or the defaults) plus the conductor's pool override."""
    voice = VOICE_DEFAULT
    tiers = _read(TIERS_FILE)
    if isinstance(tiers, dict) and tiers:
        with contextlib.suppress(LayoutError, TypeError, AttributeError):
            voice = from_tiers(tiers)
    research = dict(RESEARCH_DEFAULT)
    pool = _read(POOL_FILE)
    if isinstance(pool, list):
        research = {}
        for p in pool:
            with contextlib.suppress(LayoutError, TypeError, ValueError, AttributeError):
                research[_name(p["name"])] = int(p.get("count", 0))
    return validate({"voice": voice, "research": research})


def current(path: Path | None = None) -> tuple[dict, bool]:
    """(layout, from_file). A missing or invalid file gives the seed and False."""
    raw = _read(path or FILE)
    if raw is not None:
        try:
            return validate(raw), True
        except LayoutError:
            pass
    return seed(), False


def load(path: Path | None = None) -> dict | None:
    """The saved layout, or None when there is none (callers then keep their old behaviour)."""
    raw = _read(path or FILE)
    try:
        return validate(raw) if raw is not None else None
    except LayoutError:
        return None


def tiers(layout: dict) -> dict:
    """Voice layout -> the {tier: [binding, ...]} map tiers.race takes (one binding per lane)."""
    return {tier: [dict(MODELS[name]["binding"]) for name, n in models.items() for _ in range(n)]
            for tier, models in layout["voice"].items() if models}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write(path: Path, layout: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(layout, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # readers see the old file or the new one, never half of it
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def change(edit, by: str = "", path: Path | None = None) -> dict:
    """Read, edit (a function layout -> layout), validate and write under the lock. Returns the new layout."""
    path = path or FILE
    with _locked(path):
        layout, _ = current(path)
        new = validate(edit(json.loads(json.dumps(layout))))
        new["updated"], new["by"] = round(time.time(), 3), by[:40]
        _write(path, new)
        return new


# ---------------------------------------------------------------- actions (the Hermes tool and the call share these)
def _role(role: str | None, name: str) -> str:
    """Where a lane goes. Bare "N X lanes" means worker lanes (the TV pool): Kevin's numbers (3 haiku, 2 sonnet,
    10 sol, 2 opus) are worker counts, and 10 sol racers on the call would be 10 identical SKIPs (LANES-BACKEND.md).
    "answer"/"voice"/"call" lanes race on every spoken request, in the model's home tier unless one is named."""
    role = (role or "").lower().strip()
    if role in ("", "research", "tv", "conductor", "pool", "worker", "workers"):
        return "research"
    if role in ("voice", "call", "answer", "home"):
        return MODELS[name]["home"] if MODELS[name]["binding"] else "research"
    if role in VOICE_TIERS:
        return role
    raise LayoutError(f"unknown role {role!r}; roles: research (default, TV worker lanes), voice (answer lanes on "
                      f"the call), fast, medium, slow")


def _bucket(layout, role):
    return layout["research"] if role == "research" else layout["voice"].setdefault(role, {})


def act(action: str, model: str = "", count: int | None = None, role: str = "", by: str = "",
        path: Path | None = None) -> dict:
    """list / add / remove / set. Returns {"ok", "layout", "say", ...}; LayoutError on a bad request."""
    action = (action or "list").lower().strip()
    if action in ("list", "show", "status"):
        layout, saved = current(path)
        busy = running()
        say = describe(layout) + (f" {busy} workers running now." if busy is not None else "")
        return {"ok": True, "action": "list", "saved": saved, "layout": layout, "running": busy, "say": say}
    if action not in ("add", "remove", "set"):
        raise LayoutError("action must be list, add, remove or set")
    name = _name(model)
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise LayoutError("count must be a whole number >= 0")
    target = _role(role, name)
    if target != "research" and MODELS[name]["binding"] is None:
        target = "research"
    if action == "add" and not count:
        count = 1

    def edit(layout):
        if action == "remove" and (role or "").strip().lower() in ("voice", "call", "answer") and target != "research":
            tiers_ = [t for t in VOICE_TIERS if layout["voice"].get(t, {}).get(name)]  # "drop the sol lanes": all tiers
        else:
            tiers_ = [target]
        for t in tiers_:
            bucket = _bucket(layout, t)
            have = bucket.get(name, 0)
            if action == "add":
                bucket[name] = have + count
            elif action == "set":
                bucket[name] = count if count is not None else 1
            else:
                bucket[name] = max(0, have - count) if count else 0
            if not bucket[name] and t != "research":
                bucket.pop(name)
        return layout

    layout = change(edit, by=by, path=path)
    return {"ok": True, "action": action, "model": name, "role": target, "layout": layout,
            "say": confirm(action, name, count, target) + " " + describe(layout)}


CONDUCTOR_STATE = Path(os.environ.get("Z0LIVE_CONDUCTOR", str(Path.home() / ".local/share/z0live/conductor"))) / "state.json"


def running(path: Path | None = None) -> int | None:
    """How many TV conductor lanes run right now (None when the conductor has no state)."""
    st = _read(path or CONDUCTOR_STATE)
    if not isinstance(st, dict):
        return None
    return sum(1 for a in st.get("asks", []) for ln in a.get("lanes", []) if ln.get("state") == "running")


def confirm(action, name, count, role) -> str:
    label = MODELS[name]["label"]
    where = "worker lanes" if role == "research" else f"{role} answer lanes"
    if action == "add":
        return f"Added {count} {label} to the {where}."
    if action == "set":
        return f"{label} {where} set to {count if count is not None else 1}."
    return f"Removed {count or 'all'} {label} from the {where if role == 'research' else 'answer lanes'}."


def _count_words(models: dict) -> str:
    return ", ".join(f"{n} {name}" for name, n in models.items() if n) or "none"


def describe(layout: dict) -> str:
    """One spoken line: the answer lanes racing on the call, then the TV worker lanes."""
    voice = "; ".join(f"{tier} {_count_words(layout['voice'].get(tier, {}))}" for tier in VOICE_TIERS
                      if layout["voice"].get(tier))
    total = sum(sum(m.values()) for m in layout["voice"].values())
    workers = sum(layout["research"].values())
    return f"Answer lanes on the call: {total} ({voice}). Worker lanes: {workers} ({_count_words(layout['research'])})."


# ---------------------------------------------------------------- spoken / typed commands (precision over recall)
NUM = {"a": 1, "an": 1, "one": 1, "another": 1, "single": 1, "two": 2, "to": 2, "too": 2, "couple": 2, "pair": 2,
       "three": 3, "four": 4, "for": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_MODEL = r"(?P<model>haikus?|sonnets?|opus(?:es)?|sols?|soul|sole|saul|gpt|codex|sr|semantic router|router|fable|free)"
_N = r"(?P<n>\d{1,2}|a|an|one|another|single|two|to|too|couple(?: of)?|pair(?: of)?|three|four|for|five|six|seven|eight|nine|ten)"
_KIND = r"(?:(?P<kind>answer|voice|worker|research|tv)\s+)?"
_ROLE = (r"(?:\s+(?:to|in|on|for|into|from|off)\s+(?:the\s+|this\s+)?(?P<role>fast|medium|slow|research|tv|call|voice|"
         r"answer|worker)(?:\s+(?:tier|lane|lanes|pool))?)?")
_LEAD = r"^(?:(?:hey |ok |okay |so )?(?:z0|zero|z ?zero),?\s+)?(?:(?:please|can you|could you|would you|go ahead and|let's|lets)\s+)*"
_ADD = re.compile(_LEAD + r"(?:add|attach|give me|spin up|bring in|put on|start)\s+" + _N + r"\s+(?:more\s+)?"
                  + _MODEL + r"\s+" + _KIND + r"lanes?\b" + _ROLE + r"\W*(?:please)?\W*$", re.I)
_SET = re.compile(_LEAD + r"(?:set|make it|use|i want|go with)\s+(?:the\s+)?" + _N + r"\s+" + _MODEL + r"\s+" + _KIND
                  + r"lanes?\b"
                  + _ROLE + r"\W*$", re.I)
_SET2 = re.compile(_LEAD + r"(?:set|make)\s+(?:the\s+)?" + _MODEL + r"\s+" + _KIND + r"lanes?\s+(?:to\s+)?" + _N + _ROLE + r"\W*$", re.I)
_REMOVE = re.compile(_LEAD + r"(?:drop|remove|detach|kill|lose|get rid of|stop)\s+(?:(?:the|all(?: the)?|my)\s+)?"
                     r"(?:" + _N + r"\s+)?(?:of\s+the\s+)?" + _MODEL + r"\s+" + _KIND + r"lanes?\b" + _ROLE + r"\W*(?:please)?\W*$", re.I)
# "how many lanes (do I have|are attached|are on the call)", "what's the lane layout": the layout, not what is running.
_LIST = re.compile(_LEAD + r"(?:(?:how many lanes(?: do i have| are attached| are on (?:the|this) call| have i got)?)"
                   r"|(?:what(?:'s| is) (?:the|my) lane (?:layout|setup))|(?:list|show)(?: me)? (?:the |my )?lanes"
                   r"|(?:what lanes (?:do i have|are attached)))\W*$", re.I)


_FILLER = re.compile(r"^(?:(?:yeah|yes|yep|uh|um|uhm|so|and|ok|okay|alright|all right|for sure|now|also|then|great|cool|"
                     r"right|well|hey|oh)\b[\s,.]*)+", re.I)
_FILLER_END = re.compile(r"(?:[\s,]+(?:right now|now|please|for me|thanks|thank you|real quick))+$", re.I)


def _n(word: str | None) -> int | None:
    if not word:
        return None
    w = word.lower().split()[0]
    return int(w) if w.isdigit() else NUM.get(w)


def command(text: str) -> dict | None:
    """One spoken/typed lane command -> {"action", "model", "count", "role"}, or None for anything else.
    Whole-utterance matches only: a question, a negation or reported speech falls through to Hermes."""
    t = " ".join(str(text or "").replace("’", "'").split()).strip()
    if not t or len(t) > 300:
        return None
    t = [p for p in re.split(r"(?<=[.!?])\s+", t) if p.strip(" .!?")][-1:]  # the last sentence is the command
    t = _FILLER_END.sub("", _FILLER.sub("", t[0] if t else "").strip(" .!?,")).strip(" .!?,")
    if not t or len(t) > 120 or re.search(r"\b(don't|do not|never|didn't|shouldn't|why|said|asked|if|not)\b", t, re.I):
        return None
    if _LIST.match(t):
        return {"action": "list"}
    for action, rx in (("add", _ADD), ("set", _SET), ("set", _SET2), ("remove", _REMOVE)):
        m = rx.match(t)
        if m:
            role = (m.group("role") or m.group("kind") or "").lower()
            role = {"call": "voice", "answer": "voice", "tv": "research", "worker": "research"}.get(role, role)
            return {"action": action, "model": ALIASES.get(m.group("model").lower(), m.group("model").lower()),
                    "count": _n(m.group("n")), "role": role}
    return None


def run(text: str, by: str = "voice", path: Path | None = None) -> dict | None:
    """command() then act(): None when `text` is not a lane command; otherwise the result (ok False + say on error)."""
    cmd = command(text)
    if not cmd:
        return None
    try:
        return act(cmd["action"], cmd.get("model", ""), cmd.get("count"), cmd.get("role", ""), by=by, path=path)
    except LayoutError as exc:
        return {"ok": False, "error": str(exc), "say": "I couldn't change the lanes: " + str(exc)}
