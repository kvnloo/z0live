"""The lanes attached to the conversation: one small JSON layout that the voice tiers and the TV conductor both read.

~/.config/z0live/layout.json (z0live.layout.v1) holds model counts per role:

    {"schema": "z0live.layout.v1",
     "voice": {"fast": {"terra": 2}, "medium": {"sonnet": 1, "sol": 1}, "slow": {"opus": 1, "sol": 1}},
     "research": {"haiku": 3, "sonnet": 2, "sol": 10, "opus": 2, "sr": 0, "fable": 0}}

"voice" (answer) lanes race on every spoken request (tiers.load renders them as {tier: [binding, ...]}). "research" counts are
the conductor's worker pool for the one-shot TV lanes; a bare "add 3 sol lanes" changes those. No file means no change: tiers.load falls back to tiers.json
and the conductor to its own pool, so the first write seeds from what runs now.

Codex families (sol, astra, terra, luna) always run the newest listed version (latest_codex). Writes are validated
(known models only, caps) and atomic: a flock serializes read-modify-write across the
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
CODEX_MODELS = Path(os.environ.get("Z0LIVE_CODEX_MODELS", str(Path.home() / ".codex/models_cache.json")))
# Newest per family on 11-27 (used when the catalog cannot be read): 6.2 has only sol/astra, terra is newest at 6.1,
# luna at 6.
CODEX_FALLBACK = {"sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna"}


def latest_codex(family: str, path: Path | None = None) -> str:
    """The newest listed `gpt-<version>-<family>` in this account's Codex catalog (the owner, 11-27: "just use the latest
    version for every role"), or the 11-27 fallback. A new release (say gpt-6.3-terra) is picked up on the next start
    with no code change."""
    try:
        models = json.loads((path or CODEX_MODELS).read_text()).get("models") or []
    except (OSError, ValueError, AttributeError):
        return CODEX_FALLBACK[family]
    best = None
    for m in models:
        slug = str(m.get("slug") or "") if isinstance(m, dict) else ""
        hit = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-" + re.escape(family), slug)
        if not hit or m.get("visibility", "list") != "list":
            continue
        version = tuple(int(x) for x in hit.group(1).split("."))
        version += (0,) * (3 - len(version))
        if best is None or version > best[0]:
            best = (version, slug)
    return best[1] if best else CODEX_FALLBACK[family]


def _codex(family: str, home: str) -> dict:
    slug = latest_codex(family)
    version = slug.split("-")[1]
    return {"binding": {"provider": CODEX, "model": slug}, "home": home,
            "label": f"GPT {version if '.' in version else version + '.0'} {family.capitalize()}"}


# name -> voice binding (None: research only) and its home tier (where "add N <name> lanes" goes on the call).
MODELS = {
    "haiku": {"binding": {"provider": CLAUDE, "model": "claude-haiku-5-5"}, "home": "fast", "label": "Haiku 5.5"},
    "sonnet": {"binding": {"provider": CLAUDE, "model": "claude-sonnet-5-5"}, "home": "medium", "label": "Sonnet 5.5"},
    "opus": {"binding": {"provider": CLAUDE, "model": "claude-opus-5-5"}, "home": "slow", "label": "Opus 5.5"},
    "sol": _codex("sol", "medium"),
    "astra": _codex("astra", "slow"),
    "terra": _codex("terra", "medium"),
    "luna": _codex("luna", "fast"),
    # vllm-sr on the mbp picks a free upstream per request (groq/nim); lane_server routes it via base_url.
    "sr": {"binding": {"provider": "custom", "model": "vllm-sr/turbo", "base_url": SR_URL}, "home": "fast",
           "label": "semantic router"},
    "fable": {"binding": None, "home": "research", "label": "Haiku + Fable advisor"},
}
# Words the owner (or the speech-to-text) uses for each model.
ALIASES = {"haiku": "haiku", "haikus": "haiku", "sonnet": "sonnet", "sonnets": "sonnet", "opus": "opus",
           "opuses": "opus", "sol": "sol", "sols": "sol", "soul": "sol", "sole": "sol", "saul": "sol",
           "gpt": "sol", "codex": "sol", "astra": "astra", "astras": "astra", "astro": "astra", "terra": "terra",
           "terras": "terra", "tera": "terra", "terror": "terra", "luna": "luna", "lunas": "luna", "sr": "sr", "router": "sr", "semantic router": "sr", "free": "sr",
           "fable": "fable", "advisor": "fable"}
CAP_TIER = 6        # racing more copies than this per voice tier only burns quota
CAP_RESEARCH = 18   # per model in the TV pool (the conductor's ultra cap: 18 lanes per ask)
CAP_POOL = 30       # whole TV pool
# Seeds when neither layout.json nor the older files say otherwise (matches tiers.json and POOL_DEFAULT, 11-27).
# Fast tier is Terra + Luna, not 3 Haiku (lane_replay hill climb, 11-27): with the call's ~7k-char context, Haiku answered
# at p50 3.4 s and skipped more, while Terra took 2.05 s and Luna 1.72 s. Across 77 replayed asks, useful answers within
# 3 s rose from 0.70 to 0.78.
VOICE_DEFAULT = {"fast": {"terra": 2}, "medium": {"sonnet": 1, "sol": 1}, "slow": {"opus": 1, "sol": 1}}
# The owner's mix (11-27): 3 haiku, 2 sonnet, 5 terra, 5 sol, 2 opus, each at its newest version.
RESEARCH_DEFAULT = {"haiku": 3, "sonnet": 2, "terra": 5, "sol": 5, "opus": 2, "astra": 0, "luna": 0, "sr": 0, "fable": 0}


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
    # A versioned name ("gpt-6.1-terra", "6.2 sol") means that family: lanes always run its newest version.
    fam = re.search(r"\b(sol|astra|terra|luna)\b", w.replace("-", " "))
    if fam:
        return fam.group(1)
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
    """Where a lane goes. Bare "N X lanes" means worker lanes (the TV pool): the owner's numbers (3 haiku, 2 sonnet,
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


def main_model(layout: dict, tier: str) -> str:
    """The model a bare "N fast lanes" means: the one with most lanes in that tier now (first listed wins a tie),
    else the tier's default (Terra for fast, 11-27 hill climb)."""
    models = layout["voice"].get(tier) or {}
    if models:
        return max(models, key=lambda n: (models[n], -list(models).index(n)))
    return next(iter(VOICE_DEFAULT.get(tier) or {"haiku": 1}))


def _step(layout, action, name, count, role):
    """One validated edit of `layout` in place (no write). name may be "" for a tier word with no model."""
    if action == "tier":  # "attach five fast lanes": the tier holds exactly `count` lanes
        tier = role if role in VOICE_TIERS else ""
        if not tier:
            raise LayoutError("say which tier: fast, medium or slow")
        if count is None:
            raise LayoutError("say how many lanes")
        bucket = layout["voice"].setdefault(tier, {})
        if name:  # "... and use Haiku for all of them"
            if MODELS[name]["binding"] is None:
                raise LayoutError(f"{name} is a TV research lane only, not a voice lane")
            layout["voice"][tier] = {name: count} if count else {}
            return
        have = sum(bucket.values())
        if count == have:
            return
        if count > have:
            main = main_model(layout, tier)
            bucket[main] = bucket.get(main, 0) + count - have
        else:
            drop = have - count
            for n in sorted(bucket, key=lambda k: (bucket[k], -list(bucket).index(k))):  # fewest first
                cut = min(drop, bucket[n])
                bucket[n] -= cut
                drop -= cut
            for n in [n for n, c in bucket.items() if not c]:
                bucket.pop(n)
        return
    if not name:
        if role not in VOICE_TIERS:
            raise LayoutError("say which model, or a tier: fast, medium or slow")
        name = main_model(layout, role)
    target = _role(role, name)
    if target != "research" and MODELS[name]["binding"] is None:
        target = "research"
    if action == "add" and not count:
        count = 1
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


def act(action: str, model: str = "", count: int | None = None, role: str = "", by: str = "",
        path: Path | None = None) -> dict:
    """Apply one validated edit. `receipt` reports the saved delta; `say` also includes the layout."""
    action = (action or "list").lower().strip()
    if action in ("list", "show", "status"):
        layout, saved = current(path)
        busy = running()
        say = describe(layout) + (f" {busy} workers running now." if busy is not None else "")
        return {"ok": True, "action": "list", "saved": saved, "layout": layout, "running": busy, "say": say,
                "receipt": say}
    if action not in ("add", "remove", "set", "tier"):
        raise LayoutError("action must be list, add, remove, set or tier")
    name, role_ = (_name(model) if model else ""), (role or "").lower().strip()
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise LayoutError("count must be a whole number >= 0")
    if not name and action in ("remove", "set"):
        raise LayoutError("say which model")
    if action == "add" and not count:
        count = 1
    before = {}
    def edit(layout):
        nonlocal name
        before.update(voice={tier: dict(models) for tier, models in layout["voice"].items()},
                      research=dict(layout["research"]))
        if not name and action == "add" and role_ in VOICE_TIERS:
            name = main_model(layout, role_)
        _step(layout, action, name, count, role_)
        return layout

    layout = change(edit, by=by, path=path)
    spoken = receipt(before, layout)
    target = role_ if action == "tier" else (_role(role_, name) if name else role_)
    if target != "research" and name and MODELS[name]["binding"] is None:
        target = "research"
    return {"ok": True, "action": action, "model": name, "role": target, "layout": layout,
            "receipt": spoken, "say": (confirm(action, name, count, target) if name and action != "tier" else spoken)
            + " " + describe(layout)}


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


WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen")


def spoken_n(n: int) -> str:
    return WORDS[n] if 0 <= n < len(WORDS) else str(n)


def short(name: str) -> str:
    """The spoken model name: Haiku, Terra; the router and Fable keep their words."""
    return {"sr": "semantic router", "fable": "Fable"}.get(name, name.capitalize())


def _mix(models: dict) -> str:
    live = [(n, c) for n, c in models.items() if c]
    if len(live) == 1:
        return f"all {short(live[0][0])}" if live[0][1] > 1 else short(live[0][0])
    parts = [f"{spoken_n(c)} {short(n)}" for n, c in live]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def receipt(before: dict, after: dict) -> str:
    """One short spoken line naming exactly what changed (F3, 11-28: "attach five fast lanes" got "Let me check" and
    no word of what happened). Built from the layout as written, so it never claims more than the file holds."""
    voice = []
    for tier in VOICE_TIERS:
        old, new = (before.get("voice") or {}).get(tier) or {}, after["voice"].get(tier) or {}
        if old != new:
            n = sum(new.values())
            voice.append(f"{spoken_n(n)} {tier} lane{'s' if n != 1 else ''}" + (f", {_mix(new)}" if n else ""))
    workers = [n for n in MODELS if (before.get("research") or {}).get(n, 0) != after["research"].get(n, 0)]
    out = []
    if voice:
        out.append("Done. This call now has " + "; ".join(voice) + ".")
    if workers:
        out.append(("Done. " if not voice else "") + "The worker pool now has "
                   + ", ".join(f"{spoken_n(after['research'].get(n, 0))} {short(n)}" for n in workers)
                   + " for research asks.")
    return " ".join(out) or "No change: the lanes were already set that way."


# ---------------------------------------------------------------- spoken / typed commands (precision over recall)
# Verbs are pool edits only: "stop/kill the lanes" is the conductor's stop control (RX_STOP), and "use/start N X
# lanes" reads as an instruction for one ask (conductor explicit()), so those stay with the conductor.
NUM = {**{word: n for n, word in enumerate(WORDS)}, "zero": 0,
       "a": 1, "an": 1, "another": 1, "single": 1, "to": 2, "too": 2, "couple": 2, "pair": 2, "for": 4}
_MODEL = (r"(?P<model>haikus?|sonnets?|opus(?:es)?|sols?|soul|sole|saul|gpt|codex|astras?|astro|terras?|tera|terror|"
          r"lunas?|sr|semantic router|router|fable|free)")
_N = r"(?P<n>[0-9]{1,2}|" + "|".join(NUM) + r")(?:\s+of(?=\s+(?!the\b)))?"
_KIND = r"(?:(?P<kind>answer|voice|worker|research|tv)\s+)?"
_ROLE = (r"(?:\s+(?:to|in|on|for|into|from|off)\s+(?:the\s+|this\s+)?(?P<role>fast|medium|slow|research|tv|call|voice|"
         r"answer|worker|conversation)(?:\s+(?:tier|lane|lanes|pool))?)?")
_LEAD = r"^(?:(?:hey |ok |okay |so )?(?:z0|zero|z ?zero),?\s+)?(?:(?:please|can you|could you|would you|will you|go ahead and|let's|lets)[,\s]+)*"
_TIER_COUNT = re.compile(_LEAD + r"(?:add|attach|give me|bring in|put on|set|make it)\s+" + _N
                         + r"\s+(?P<tier>fast|medium|slow)\s+lanes?(?:\s+(?:to|on|in|for)\s+(?:the |this |my )?"
                         r"(?:call|conversation))?\W*$", re.I)
_TIER_MORE = re.compile(_LEAD + r"(?:add|attach|give me|bring in|put on)\s+" + _N
                        + r"\s+more\s+(?P<tier>fast|medium|slow)\s+lanes?(?:\s+(?:to|on|in|for)\s+"
                        r"(?:the |this |my )?(?:call|conversation))?\W*$", re.I)
_ADD = re.compile(_LEAD + r"(?:add|attach|give me|spin up|bring in|put on)\s+" + _N + r"\s+(?:more\s+)?"
                  + _MODEL + r"\s+" + _KIND + r"lanes?\b" + _ROLE + r"\W*(?:please)?\W*$", re.I)
_SET = re.compile(_LEAD + r"(?:set|make it)\s+(?:the\s+)?" + _N + r"\s+" + _MODEL + r"\s+" + _KIND
                  + r"lanes?\b"
                  + _ROLE + r"\W*$", re.I)
_SET2 = re.compile(_LEAD + r"(?:set|make)\s+(?:the\s+)?" + _MODEL + r"\s+" + _KIND + r"lanes?\s+(?:to\s+)?" + _N + _ROLE + r"\W*$", re.I)
_REMOVE = re.compile(_LEAD + r"(?:drop|remove|detach|lose|get rid of)\s+(?:(?:the|all(?: the)?|my)\s+)?"
                     r"(?:" + _N + r"\s+)?(?:of\s+the\s+)?" + _MODEL + r"\s+" + _KIND + r"lanes?\b" + _ROLE + r"\W*(?:please)?\W*$", re.I)
# "how many lanes (do I have|are attached|are on the call)", "what's the lane layout": the layout, not what is running.
_LIST = re.compile(_LEAD + r"(?:(?:how many lanes(?: do i have| are attached| are on (?:the|this) call| have i got)?)"
                   r"|(?:what(?:'s| is) (?:the|my) lane (?:layout|setup))|(?:list|show)(?: me)? (?:the |my )?lanes"
                   r"|(?:what lanes (?:do i have|are attached)))\W*$", re.I)


_FILLER = re.compile(r"^(?:(?:yeah|yes|yep|uh|um|uhm|so|and|ok|okay|alright|all right|for sure|sure|sounds good|got it|now|also|then|great|cool|"
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
    parts = [p for p in re.split(r"(?<=[.!?])\s+", t) if p.strip(" .!?")]
    # The last sentence is the command, and only when everything before it is filler ("Yeah, for sure."):
    # "Research the boat sandboxes. Use a sol lane." is a research ask, not a pool edit.
    if not parts or any(_FILLER.sub("", p).strip(" .!?,") for p in parts[:-1]):
        return None
    t = _FILLER_END.sub("", _FILLER.sub("", parts[-1]).strip(" .!?,")).strip(" .!?,")
    if not t or len(t) > 120 or re.search(r"\b(don't|do not|never|didn't|shouldn't|why|said|asked|if|not)\b", t, re.I):
        return None
    if _LIST.match(t):
        return {"action": "list"}
    for action, rx in (("tier", _TIER_COUNT), ("add", _TIER_MORE)):
        if m := rx.match(t):
            return {"action": action, "count": _n(m.group("n")), "role": m.group("tier").lower()}
    for action, rx in (("add", _ADD), ("set", _SET), ("set", _SET2), ("remove", _REMOVE)):
        m = rx.match(t)
        if m:
            role = (m.group("role") or m.group("kind") or "").lower()
            role = {"call": "voice", "conversation": "voice", "answer": "voice", "tv": "research", "worker": "research"}.get(role, role)
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
    except OSError:
        return {"ok": False, "error": "layout storage unavailable", "say": "I couldn't save the lane layout."}
