"""Blockers out loud when the owner can't see the TV (the owner 11-28: "keep surfacing blockers for me").

The TV server (atlas-engine apps/z0live-tv serve.py, loopback GET /api/needs) owns the list: every blocked kanban card
waiting on the owner, each with a ready-to-say clause, plus tv.present (TV power on, TV app polling, not marked away).
While the TV shows them (the NEEDS YOU band on every page), nothing is spoken and the cards count as seen. When the TV
is away, cards he has not seen or heard within REMEMBER_S (across calls, MEMORY file) are spoken:
  * the first time in a call: one summary line (how many wait, the top two unheard, how to open the page);
  * after that: one line when a card newly appears.
Nothing is said before the call's first exchange (the voice never speaks first), and every line goes through the
call's one status budget (Call.take_status): never over the owner or a reply, never inside STATUS_GAP of another status
line. A line that cannot be said now waits for the next poll.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import ClientTimeout

LOG = logging.getLogger("hermes.mobile.needs_voice")
URL = os.environ.get("Z0LIVE_NEEDS_URL", "http://127.0.0.1:8793/api/needs")
POLL_S = float(os.environ.get("Z0LIVE_NEEDS_POLL_S", "20"))
ON = os.environ.get("Z0LIVE_NEEDS_VOICE", "1") != "0"
MEMORY = Path(os.environ.get("Z0LIVE_NEEDS_MEMORY", str(Path.home() / ".local/share/z0live/needs-voice.json")))
REMEMBER_S = float(os.environ.get("Z0LIVE_NEEDS_REMEMBER_S", str(6 * 3600)))


def _say(r):
    return r.get("say") or r.get("what") or r["id"]


def summary(rows, fresh):
    n = len(rows)
    # The count is its own sentence and the examples are named as examples: a run that said "Top: A; and B" was
    # retold by the voice as "two things need you" while 33 did.
    head = f"While you're away from the TV: {n} blocked card{'s' if n != 1 else ''} wait{'s' if n == 1 else ''} on you."
    top = fresh[:2]
    lead = "The one you haven't heard" if len(top) == 1 else f"The top two of the {n}"
    return " ".join([head, f"{lead}: " + "; and ".join(_say(r) for r in top) + ".",
                     "Say show me blockers when you're back at the TV."])


def new_line(rows):
    if len(rows) == 1:
        return "New blocker for you: " + _say(rows[0]) + "."
    return f"{len(rows)} new blockers for you. First: " + _say(rows[0]) + "."


def _ids(rows):
    return {i for r in rows for i in [r["id"]] + list(r.get("dups") or [])}


class Watch:
    """Pure state: feed it each /api/needs payload, get back (text, ids) to say now, or None.
    memory = {card id: when the owner last saw it on the TV or heard it}, shared across calls."""

    def __init__(self, memory=None):
        self.memory = memory if memory is not None else {}
        self.summarised = False
        self.dirty = False

    def recent(self, now):
        return {i for i, t in self.memory.items() if now - t < REMEMBER_S}

    def mark(self, ids, now):
        for i in ids:
            self.memory[i] = now
        if ids:
            self.dirty = True

    def step(self, payload, now=None):
        now = time.time() if now is None else now
        rows = [r for r in payload.get("rows") or [] if r.get("id")]
        recent = self.recent(now)
        if (payload.get("tv") or {}).get("present", True):
            self.mark(_ids(rows) - recent, now)  # on the TV's NEEDS YOU band: he has them
            return None
        fresh = [r for r in rows if not ({r["id"]} | set(r.get("dups") or [])) <= recent]
        if not fresh:
            return None
        if not self.summarised:
            return summary(rows, fresh), _ids(fresh)
        return new_line(fresh), _ids(fresh)

    def said(self, ids, now=None):
        self.mark(ids, time.time() if now is None else now)
        self.summarised = True


def load_memory(path=None):
    try:
        data = json.loads((path or MEMORY).read_text())
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_memory(memory, path=None, now=None):
    now = time.time() if now is None else now
    path = path or MEMORY
    try:
        keep = {k: v for k, v in memory.items() if now - v < 7 * 86400}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep))
        tmp.replace(path)
    except OSError as exc:
        LOG.warning("needs memory not saved: %s", type(exc).__name__)


async def fetch(http):
    try:
        async with http.get(URL, timeout=ClientTimeout(total=5)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:  # noqa: BLE001 - the TV server being down must never touch the call
        return None


async def speak(call, text):
    if call.leg:
        if not call.leg.ready or call.leg.ws is None or call.leg.ws.closed:
            return False
        await call.leg.note("Brief update from Hermes about blockers waiting on the owner. Say this aloud: " + text,
                            call.trace.turn_id)
        await call.leg.raw({"type": "response.create"})
        return True
    if not call.provider_ready or call.provider_dc is None or call.provider_dc.readyState != "open":
        return False
    for event in call.protocol.from_mobile({"type": "session.commentary.append", "content": text}):
        call.send(event, to_provider=True)
    return True


async def run(call):
    """One per call; share serve.py's heartbeat verdict with the result reader."""
    w = Watch(load_memory()) if ON else None
    while not call.closed:
        payload = await fetch(call.manager.http)
        if call.closed:
            return
        call.observe_tv_presence(payload.get("tv") if isinstance(payload, dict) else None)
        if w is not None and isinstance(payload, dict) and call.protocol.exchanges:
            await poll_blockers(call, w, payload)
        await asyncio.sleep(POLL_S)


async def poll_blockers(call, w, payload):
    w.memory.update({k: v for k, v in load_memory().items() if v > w.memory.get(k, 0)})  # another call's marks
    out = w.step(payload)
    if w.dirty and not out:
        save_memory(w.memory)
        w.dirty = False
    if not out or not call.take_status("needs"):
        return  # floor busy or inside the status gap: try again next poll
    text, ids = out
    try:
        ok = await speak(call, text)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("needs line failed id=%s: %s", call.id, type(exc).__name__)
        ok = False
    if ok:
        w.said(ids)
        save_memory(w.memory)
        w.dirty = False
    call.trace("needs.spoken" if ok else "needs.dropped", cards=len(ids), why=(payload.get("tv") or {}).get("why"),
               text=text[:300])
