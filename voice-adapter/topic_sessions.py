"""One warm Hermes chat per project group for a call's requests, not a fresh session per request.

Before this, every request handed off on a call ran as a dashboard background agent (prompt.background), and Hermes
mints a new bg_* session for each one: 86 on 11-28 alone ("Grok OAuth sign-in link" #1 to #15, "Optimize voice call
setup with advisor lanes" #1 to #31), each starting cold. The owner asked twice (11-27 12:54, 11-28 12:36) for one
session per project instead of one per request.

Now a request is classified into a project group (z0live calls, Grok/xAI, TV dashboard, ...) and submitted as a turn
of that group's chat, found by its exact title through the dashboard's own title lookup (session.list {title}) and
created once with session.create {title, cwd} when missing. Follow-ups about the same project see the earlier turns,
and the chat shows in the app under its project (cwd). Hermes compacts a long chat itself.

Groups come from DEFAULT_GROUPS, or ~/.config/z0live/topic-groups.json when present:
    {"groups": [{"key": "grok", "label": "Grok and xAI", "aliases": ["grok", "xai"], "cwd": "~/workspace/repos/z0live"}]}
A request that names no group stays with the call's last group for STICKY_SECONDS (a follow-up such as "send me the
link"), else it goes to the "general" group. Z0LIVE_TOPIC_SESSIONS=0 restores one background agent per request.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import sys

# On by default in the service. Under unittest it is off unless Z0LIVE_TOPIC_SESSIONS is set, so the suite's tests of
# the per-request paths (background agent, chat turn, host) keep testing them; topic tests turn it on themselves.
_SET = os.environ.get("Z0LIVE_TOPIC_SESSIONS")
ENABLED = _SET != "0" if _SET is not None else "unittest" not in sys.modules
STICKY_SECONDS = float(os.environ.get("Z0LIVE_TOPIC_STICKY_SECONDS", "300"))
# How many of a group's requests may wait in its chat at once (the running turn plus queued turns). Past that, a
# request runs as a background agent beside the group's chat, so a burst never waits minutes behind one long turn.
DEPTH = int(os.environ.get("Z0LIVE_TOPIC_DEPTH", "2"))
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


def classify(request: str, groups: tuple[Group, ...] = DEFAULT_GROUPS, prior: str = "",
             prior_age: float | None = None) -> tuple[Group, str]:
    """(group, why) for one request. why: named (aliases hit), sticky (a follow-up within STICKY_SECONDS of the
    call's last group), general. A tie goes to the call's last group, then to the earlier group in the table (the
    more specific ones come first: "the TV remote" is the TV, not a call)."""
    by_key = {g.key: g for g in groups}
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
    groups: tuple[Group, ...] = field(default_factory=lambda: load_groups())
    last: str = ""
    last_at: float = float("-inf")
    chats: dict[str, str] = field(default_factory=dict)  # group key -> stored session id
    inflight: dict[str, int] = field(default_factory=dict)

    def route(self, request: str, now: float | None = None) -> tuple[Group, str]:
        now = time.monotonic() if now is None else now
        group, why = classify(request, self.groups, self.last, now - self.last_at)
        if group.key != GENERAL:
            self.last, self.last_at = group.key, now
        return group, why

    def admit(self, key: str) -> bool:
        """True (and counted) when the group's chat takes the request; False sends it beside the chat instead."""
        if self.inflight.get(key, 0) >= DEPTH:
            return False
        self.inflight[key] = self.inflight.get(key, 0) + 1
        return True

    def release(self, key: str):
        self.inflight[key] = max(0, self.inflight.get(key, 0) - 1)


class NotStarted(RuntimeError):
    """Nothing was submitted to the group's chat (dashboard down, lookup or create refused): another path may run it."""


async def ask_topic(http, ws_url: str, token: str, profile: str, group: Group, compose, known: str = "",
                    on_submit=None) -> tuple[str, str, bool]:
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
                    raise RuntimeError(f"{method}: {frame['error'].get('message', frame['error'])}"[:300])
                return frame.get("result") or {}
        raise RuntimeError(f"{method}: socket closed")

    try:
        try:
            stored = known
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
            text = compose(created)
            status = (await rpc("prompt.submit", {"session_id": live, "text": text, "queued": True, **scope})
                      ).get("status")
        except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
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
            elif p.get("type") == "message.complete" and started:
                answer = (p.get("payload") or {}).get("text")
                answer = answer if isinstance(answer, str) else json.dumps(answer)
                if not answer.strip():
                    raise RuntimeError("the group chat's turn ended without an answer")
                return answer.strip(), stored, created
        raise RuntimeError("chat socket closed before the group chat's turn finished")
    finally:
        await ws.close()


def busy_error(detail: str) -> bool:
    """An api_server refusal because the session already has a turn running (HTTP 409 session_active_turn)."""
    return "409" in detail or "session_active_turn" in detail


def day_session(prefix: str, group: Group, when: float | None = None) -> str:
    """A stable api_server session id per group and day (guest legs, ask @bot): one for "what did you work on today"
    across the day's calls, not one per call."""
    return f"{prefix}-{group.key}-{time.strftime('%Y%m%d', time.localtime(when))}"[:120]
