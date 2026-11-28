"""One warm Hermes chat per project group for a call's requests, not a fresh session per request.

Before this, every request handed off on a call ran as a dashboard background agent (prompt.background), and Hermes
mints a new bg_* session for each one: 86 on 11-28 alone ("Grok OAuth sign-in link" #1 to #15, "Optimize voice call
setup with advisor lanes" #1 to #31), each starting cold. Kevin, 11-27 12:54 and again 11-28 12:36: "we shouldn't be
starting a new session for every request ... a new session for every project maybe".

Now a request is classified into a project group (z0live calls, Grok/xAI, TV dashboard, ...) and submitted as a turn
of that group's chat, found by its exact title through the dashboard's own title lookup (session.list {title}) and
created once with session.create {title, cwd} when missing. Follow-ups about the same project see the earlier turns,
and the chat shows in the app under its project (cwd). Hermes compacts a long chat itself.

Groups come from DEFAULT_GROUPS, or ~/.config/z0live/topic-groups.json when present:
    {"groups": [{"key": "grok", "label": "Grok and xAI", "aliases": ["grok", "xai"], "cwd": "~/workspace/repos/z0live"}]}
A request that names no group stays with the call's last group for STICKY_SECONDS (a follow-up such as "send me the
link"), else it goes to the "general" group. Z0LIVE_TOPIC_SESSIONS=0 restores one background agent per request.

Finer than a group: a request naming a kanban card (t_1a2b3c4d) gets that card's own chat, one naming a device (s22,
lenovo, mbp, ...) that device's own chat, and one naming a repo under ~/workspace/repos that no group covers that
repo's own chat (a repo a group already points at, by cwd, is that group). So asks about the same repo share one warm
chat, and two different cards, devices or repos never share one.

Concurrency: a group's chat is looked up or created under one lock per title (ask_topic), so a burst of first asks
makes one chat, not one each; the dashboard queues their turns (queued: true). The api_server legs (guest, ask @bot)
hold the day session under hold() / session_flock() so concurrent asks run one after another in it; a request that
cannot get the session within its wait runs in its call's own session, never dropped.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import itertools
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ENABLED = os.environ.get("Z0LIVE_TOPIC_SESSIONS", "1") != "0"
STICKY_SECONDS = float(os.environ.get("Z0LIVE_TOPIC_STICKY_SECONDS", "300"))
# How many of a group's requests may wait in its chat at once (the running turn plus queued turns). Past that, a
# request runs as a background agent beside the group's chat, so a burst never waits minutes behind one long turn.
# 3: a burst of three asks about one project (the 11-28 "grok link" x3) shares the one warm chat.
DEPTH = int(os.environ.get("Z0LIVE_TOPIC_DEPTH", "3"))
# How long an api_server ask waits for its day session (another call's turn in it) before using its call's own.
HOLD_WAIT = float(os.environ.get("Z0LIVE_TOPIC_HOLD_WAIT", "60"))
TITLE_PREFIX = "z0live · "
GENERAL = "general"
CONFIG = Path(os.environ.get("Z0LIVE_TOPIC_GROUPS", str(Path.home() / ".config/z0live/topic-groups.json")))


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    cwd: str = ""

    @property
    def title(self) -> str:
        return TITLE_PREFIX + self.label


REPOS = "~/workspace/repos"
REPO_ROOT = Path(os.path.expanduser(os.environ.get("Z0LIVE_TOPIC_REPOS", REPOS)))
CARD = re.compile(r"(?<![A-Za-z0-9_])t_[0-9a-f]{8}(?![A-Za-z0-9_])", re.I)
# Devices with a chat each. groot is its own group; "pc" is a bot profile, not a device here.
DEVICES = {"s22": "S22", "s25": "S25", "lenovo": "Lenovo tablet", "mbp": "MacBook Pro", "macbook": "MacBook Pro",
           "mac studio": "Mac Studio", "quest": "Quest"}
DEFAULT_GROUPS = (
    Group("grok", "Grok and xAI", ("grok", "xai", "x ai", "x.ai"), f"{REPOS}/z0live"),
    Group("dash", "Dash, S Pen and Ripple", ("dash app", "dash", "s pen", "spen", "ripple", "annotate", "annotation",
                                              "annotating"), ""),
    Group("meetings", "Meetings", ("meeting", "meetings", "livekit", "google meet", "fframes", "launch video"), ""),
    Group("tv", "TV dashboard", ("tv", "television", "dashboard", "atlas", "remote", "factory page", "ephemeral",
                                 "finding page", "needs page", "threads need you", "screen"), f"{REPOS}/atlas-engine-tv"),
    Group("phones", "Phones and cameras", ("s22", "s25", "lenovo", "phone", "camera", "droidcam", "ultra wide",
                                           "ultrawide", "lens", "adb"), ""),
    Group("groot", "groot", ("groot", "cachyos", "3080", "gpu"), ""),
    Group("tokens", "Tokens and routing", ("token", "tokens", "tokenomics", "routing", "router", "semantic router",
                                           "eval", "evals", "hill climb", "hillclimb", "local model", "local models",
                                           "boat"), f"{REPOS}/home"),
    Group("z0live", "z0live calls", ("z0live", "zero live", "0 live", "zero-live", "call", "calls", "voice", "lane",
                                     "lanes", "fable", "advisor", "advisors", "multi-session", "session", "sessions",
                                     "conductor", "gpt live", "gpt-live"), f"{REPOS}/z0live"),
    Group("hermes", "Hermes", ("hermes", "gateway", "delegate", "delegation", "kanban", "card", "cards", "skill",
                               "skills", "memory", "memories", "profile", "cron", "upstream", "pr"), ""),
    Group(GENERAL, "general", (), ""),
)
GENERAL_GROUP = DEFAULT_GROUPS[-1]


def load_groups(path: Path | None = None) -> tuple[Group, ...]:
    """The configured groups, always ending with "general"; DEFAULT_GROUPS when the file is missing or bad."""
    try:
        raw = json.loads((path or CONFIG).read_text())
        groups = tuple(Group(str(g["key"]), str(g.get("label") or g["key"]),
                             tuple(str(a).lower() for a in g.get("aliases") or ()), str(g.get("cwd") or ""))
                       for g in raw["groups"] if g.get("key") and g["key"] != GENERAL)
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_GROUPS
    return groups + (Group(GENERAL, "general"),) if groups else DEFAULT_GROUPS


def _pattern(alias: str) -> re.Pattern:
    # Letters and digits bound a word; "_" and "-" do not ("delegate_task" names delegate, "z0live-tv" names z0live).
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias).replace(r"\ ", r"[\s\-]+") + r"(?![A-Za-z0-9])", re.I)


_PATTERNS: dict[str, re.Pattern] = {}


def score(group: Group, text: str) -> int:
    hits = 0
    for alias in group.aliases:
        pat = _PATTERNS.get(alias) or _PATTERNS.setdefault(alias, _pattern(alias))
        hits += len(pat.findall(text))
    return hits


def repo_names(root: Path | None = None) -> tuple[str, ...]:
    try:
        return tuple(sorted((p.name for p in (root or REPO_ROOT).iterdir() if p.is_dir() and len(p.name) >= 4),
                            key=len, reverse=True))
    except OSError:
        return ()


def specific(request: str, groups: tuple[Group, ...], repos: tuple[str, ...] | None = None) -> Group | None:
    """The card, device or repo the request names, as its own group; None when it names none."""
    text = request or ""
    card = CARD.search(text)
    if card:
        cid = card.group(0).lower()
        return Group("card-" + cid, "card " + cid)
    for alias, label in DEVICES.items():
        if (_PATTERNS.get(alias) or _PATTERNS.setdefault(alias, _pattern(alias))).search(text):
            return Group("device-" + alias.replace(" ", "-"), label)
    aliases = {a for g in groups for a in g.aliases}
    for name in (repos if repos is not None else repo_names()):
        if name.lower() in aliases:
            continue  # "z0live" is the z0live group's word, not a repo pick
        # Only a name that cannot be an ordinary word: it has a digit or a hyphen, or "repo"/"project" beside it.
        word = re.escape(name).replace(r"\-", r"[\s\-]+")
        plain = not re.search(r"[\d\-]", name)
        pat = (r"(?:repo|project)\s+" + word + r"(?![A-Za-z0-9])|(?<![A-Za-z0-9])" + word + r"\s+(?:repo|project)"
               if plain else r"(?<![A-Za-z0-9])" + word + r"(?![A-Za-z0-9\-])")
        if re.search(pat, text, re.I):
            for g in groups:  # a repo a group already points at is that group: one chat per repo
                if g.cwd and Path(os.path.expanduser(g.cwd)).name == name:
                    return g
            return Group("repo-" + name.lower(), name, (), str(REPO_ROOT / name))
    return None


def classify(request: str, groups: tuple[Group, ...] = DEFAULT_GROUPS, prior: "str | Group" = "",
             prior_age: float | None = None, repos: tuple[str, ...] | None = ()) -> tuple[Group, str]:
    """(group, why) for one request. why: card / device / repo (names one: its own chat), named (aliases hit),
    sticky (a follow-up within STICKY_SECONDS of the call's last group), general. A tie goes to the call's last group,
    then to the earlier group in the table (the more specific ones come first: "the TV remote" is the TV, not a call).
    repos: the repo names to look for (None: the folders under REPO_ROOT; () skips repos)."""
    by_key = {g.key: g for g in groups}
    if isinstance(prior, Group):
        by_key.setdefault(prior.key, prior)
        prior = prior.key
    pick = specific(request, groups, repos)
    if pick is not None:
        why = pick.key.split("-", 1)[0] if pick.key.startswith(("card-", "device-", "repo-")) else "repo"
        return pick, why
    scored = [(score(g, request or ""), -i, g) for i, g in enumerate(groups) if g.key != GENERAL]
    best = max((s for s, _, _ in scored), default=0)
    if best > 0:
        top = [g for s, _, g in scored if s == best]
        pick = by_key[prior] if prior in {g.key for g in top} else top[0]
        return pick, "named"
    if prior and prior in by_key and prior_age is not None and prior_age <= STICKY_SECONDS:
        return by_key[prior], "sticky"
    return by_key.get(GENERAL) or Group(GENERAL, "general"), "general"


@dataclass
class Router:
    """Per call: which group each request went to, the group chats found or made, and how many of each group's
    requests are in its chat right now."""
    groups: tuple[Group, ...] = field(default_factory=load_groups)
    last: str = ""
    last_at: float = float("-inf")
    last_group: Group | None = None
    repos: tuple[str, ...] | None = None  # None: the folders under REPO_ROOT
    chats: dict[str, str] = field(default_factory=dict)  # group key -> stored session id
    inflight: dict[str, int] = field(default_factory=dict)

    def route(self, request: str, now: float | None = None) -> tuple[Group, str]:
        now = time.monotonic() if now is None else now
        prior = self.last_group if self.last_group and self.last_group.key == self.last else self.last
        group, why = classify(request, self.groups, prior, now - self.last_at, self.repos)
        if group.key != GENERAL:
            self.last, self.last_at, self.last_group = group.key, now, group
        return group, why

    def admit(self, key: str) -> bool:
        """True (and counted) when the group's chat takes the request; False sends it beside the chat instead."""
        if self.inflight.get(key, 0) >= DEPTH:
            return False
        self.inflight[key] = self.inflight.get(key, 0) + 1
        return True

    def release(self, key: str):
        self.inflight[key] = max(0, self.inflight.get(key, 0) - 1)


_OPENING: dict[tuple, asyncio.Lock] = {}
_TITLED: dict[tuple, str] = {}  # (ws url, profile, title) -> stored id this process found or made


def _opening(key: tuple) -> asyncio.Lock:
    lock = _OPENING.get(key)
    if lock is None:
        lock = _OPENING[key] = asyncio.Lock()
    return lock


class NotStarted(RuntimeError):
    """Nothing was submitted to the group's chat (dashboard down, lookup or create refused): another path may run it."""


class Refused(RuntimeError):
    """An explicit JSON-RPC refusal, distinct from losing the socket after sending a request."""


async def ask_topic(http, ws_url: str, token: str, profile: str, group: Group, compose, known: str = "",
                    on_submit=None, on_delta=None) -> tuple[str, str, bool]:
    """Submit one request as a turn of the group's chat; (final text, stored session id, created).

    The chat is `known` (this call already used it) or found by its exact title (session.list {title}, which resolves
    a compacted chat to its live tip), else created with that title and the group's cwd. compose(created) gives the
    turn's text: a new chat starts empty, so it gets the call's memory. A chat whose turn is running gets the request
    queued after it (queued: true): never a steer into the running turn (114034). Errors before the submit raise
    NotStarted; after it, the request ran (or is running), so the caller must not run it again."""
    ids = itertools.count(1)
    headers = {"X-Hermes-Profile": profile} if profile else {}
    params = {"token": token, **({"profile": profile} if profile else {})}
    scope = {"profile": profile} if profile else {}
    try:
        ws = await http.ws_connect(ws_url, params=params, headers=headers, heartbeat=30, max_msg_size=0)
    except Exception as exc:  # noqa: BLE001 - nothing was submitted
        raise NotStarted(f"dashboard socket: {type(exc).__name__}: {exc}"[:300]) from exc

    async def rpc(method, p):
        rid = next(ids)
        await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": p}))
        async for msg in ws:
            frame = json.loads(msg.data)
            if frame.get("id") == rid:
                if frame.get("error"):
                    raise Refused(f"{method}: {frame['error'].get('message', frame['error'])}"[:300])
                return frame.get("result") or {}
        raise RuntimeError(f"{method}: socket closed")

    where = (ws_url, profile, group.title)
    opening = _opening(where)
    acquired = False
    submitting = False
    try:
        try:
            # One voice turn at a time per project. The event stream is scoped to the session, not to a submit ID.
            await opening.acquire()
            acquired = True
            stored = known or _TITLED.get(where, "")
            if not stored:
                found = ((await rpc("session.list", {"title": group.title, **scope})).get("sessions") or [{}])[0]
                stored = found.get("resolved_id") or found.get("id") or ""
            created = not stored
            if stored:
                live = (await rpc("session.resume", {"session_id": stored, "omit_messages": True,
                                                     "inline_images": False, "lazy": True, **scope})
                        ).get("session_id") or stored
            else:
                cwd = os.path.expanduser(group.cwd) if group.cwd else ""
                made = await rpc("session.create", {"title": group.title, **scope,
                                                    **({"cwd": cwd} if cwd and os.path.isdir(cwd) else {})})
                live, stored = made["session_id"], made.get("stored_session_id") or made["session_id"]
            _TITLED[where] = stored
            text = compose(created)
            submitting = True
            status = (await rpc("prompt.submit", {"session_id": live, "text": text, "queued": True, **scope})
                      ).get("status")
        except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
            if submitting and not isinstance(exc, Refused):
                raise RuntimeError("project prompt submission could not be confirmed") from exc
            raise NotStarted(str(exc)[:300]) from exc
        if on_submit:
            on_submit(stored, created, status)
        started = status != "queued"  # a queued turn begins after the running turn's message.complete
        async for msg in ws:
            frame = json.loads(msg.data)
            p = frame.get("params") or {}
            if frame.get("method") != "event" or p.get("session_id") != live:
                continue
            if p.get("type") == "message.start":
                started = True
            elif p.get("type") == "message.delta" and started and on_delta:
                await on_delta((p.get("payload") or {}).get("text"))
            elif p.get("type") == "message.complete" and started:
                answer = (p.get("payload") or {}).get("text")
                answer = answer if isinstance(answer, str) else json.dumps(answer)
                if not answer.strip():
                    raise RuntimeError("the group chat's turn ended without an answer")
                return answer.strip(), stored, created
        raise RuntimeError("chat socket closed before the group chat's turn finished")
    finally:
        if acquired:
            opening.release()
        await ws.close()


def busy_error(detail: str) -> bool:
    """An api_server refusal because the session already has a turn running (HTTP 409 session_active_turn)."""
    return "409" in detail or "session_active_turn" in detail


def day_session(prefix: str, group: Group, when: float | None = None) -> str:
    """A stable api_server session id per group and day (guest legs, ask @bot): one for "what did you work on today"
    across the day's calls, not one per call."""
    return f"{prefix}-{group.key}-{time.strftime('%Y%m%d', time.localtime(when))}"[:120]


# ---- holding a day session (api_server legs: guest, ask @bot) ----
_HELD: dict[str, asyncio.Lock] = {}


@contextlib.asynccontextmanager
async def hold(session: str, wait: float):
    """Yields True with the session held by this process (other asks for it wait their turn), or False after `wait`
    seconds: the caller then runs in its own session, so a long turn never drops the request behind it."""
    lock = _HELD.get(session)
    if lock is None:
        lock = _HELD[session] = asyncio.Lock()
    try:
        await asyncio.wait_for(lock.acquire(), wait)
    except asyncio.TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


@contextlib.contextmanager
def session_flock(folder: Path, session: str, wait: float, poll: float = 0.1):
    """hold() across processes and threads (ask_bot runs in a thread or as the CLI): an flock per session id."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / (re.sub(r"[^A-Za-z0-9_.-]", "_", session) + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sticky_load(path: Path, who: str, groups: tuple[Group, ...], now: float | None = None) -> tuple[Group | None, float]:
    """(the last group `who` asked about, its age in seconds) from a small json file; (None, inf) when none."""
    try:
        rec = json.loads(path.read_text()).get(who) or {}
        g = Group(str(rec["key"]), str(rec.get("label") or rec["key"]), (), str(rec.get("cwd") or ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, float("inf")
    g = next((x for x in groups if x.key == g.key), g)
    return g, (time.time() if now is None else now) - float(rec.get("at") or 0)


def sticky_save(path: Path, who: str, group: Group, now: float | None = None):
    if group.key == GENERAL:
        return
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[who] = {"key": group.key, "label": group.label, "cwd": group.cwd, "at": time.time() if now is None else now}
    # Best effort, never raises: the ask goes on without a sticky topic. A unique temp file per write, since asks
    # run in threads of one process (a per-pid name raced: one thread's replace took the other's file).
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
