"""No "started" without a receipt, and a ledger of what the voice promised (11-27 call audit).

On 11-27 the call heard "I've started them" / "I'll start it now" for hill climbs that never ran: the lanes had died in
8 s, or nothing had been launched at all, and no one owned the promise afterwards. Two pure pieces:

- guard(text, receipt): a line the adapter is about to speak (a Hermes answer, a tier lane, a lane card) that claims
  it started or launched something keeps that claim only with a receipt: a running lane or launch of this request on
  the conductor, a kanban task created since the request that a worker claimed, or a job id in the line itself (a
  kanban task t_..., a run id, a PR). Otherwise the claiming sentence is
  replaced with an honest one. The voice model's own words cannot be rewritten here: its persona carries the same rule.
- promises(text): the "I'll start / add / fix ..." sentences in the voice's own reply. Mobile_live records each as
  promise.made and writes every one to PROMISES at call close with the lanes that followed it for its request; one
  with none is promise.unowned (owner null), so a promise has a record to look at after the call.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

PROMISES = Path(os.environ.get("Z0LIVE_PROMISES", str(Path.home() / ".local/share/z0live/promises.jsonl")))

# first person only: "round 14 is running now" reports someone else's job, "I've started them" claims a launch
CLAIM = re.compile(r"\b(?:I(?:'ve| have)?\s+(?:just\s+|now\s+|already\s+)?(?:started(?!\s+(?:on|looking|checking|"
                   r"reading|working|digging|going|to)\b)|kicked(?:\s+\w+)?\s+off|launched|"
                   r"spun\s+up|restarted|set\s+(?:it|them)\s+running)|I'll\s+start\s+(?:it|them|that)\s+now|"
                   r"I'm\s+starting\s+(?:it|them|that)|starting\s+(?:it|them)\s+now)\b", re.I)
JOB_ID = re.compile(r"\b(?:t_[0-9a-f]{6,}|run\s+(?:id\s+)?[#\w-]{5,}|PR\s*#?\d+|pull request\s*#?\d+|"
                    r"task\s+t_\w+|job\s+[#\w-]{5,})\b", re.I)
PROMISE = re.compile(r"[^.?!]*\b(?:I'll|I will|I'm going to|I am going to)\s+(?:\w+\s+)?(?:start|kick|launch|add|put|"
                     r"set up|spin up|restart|run|build|fix|file|open|create|make|queue)\b[^.?!]*[.?!]?", re.I)
KANBAN_DB = Path(os.environ.get("Z0LIVE_KANBAN_DB", str(Path.home() / ".hermes/kanban.db")))
HONEST = "I've asked for it, but I have no receipt that it's running yet."


def kanban_started(since: float, db: Path | None = None) -> bool:
    """A kanban task created at/after `since` that a worker has claimed (Hermes starts its workers there): read-only."""
    try:
        con = sqlite3.connect(f"file:{db or KANBAN_DB}?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute("select 1 from tasks where created_at >= ? and (status in ('running','review','done') "
                              "or worker_started_at or last_heartbeat_at or started_at) limit 1", (int(since),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return row is not None


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split())) if s]


def claims_started(text: str) -> bool:
    return bool(CLAIM.search(str(text or "")))


def guard(text: str, receipt: bool) -> tuple[str, bool]:
    """(text to speak, rewritten?). A start claim stays when there is a receipt or its sentence names a job id."""
    if receipt or not claims_started(text):
        return text, False
    out, changed = [], False
    for s in sentences(text):
        if CLAIM.search(s) and not JOB_ID.search(s):
            changed = True
            if HONEST not in out:
                out.append(HONEST)
        else:
            out.append(s)
    return (" ".join(out), True) if changed else (text, False)


def promises(text: str) -> list[str]:
    """The promise sentences in the voice's own reply ("I'll add it as its own item"), not "I'll check"."""
    return [" ".join(m.group(0).split())[:240] for m in PROMISE.finditer(str(text or "")) if m.group(0).strip()]


def write_ledger(call: str, made: list[dict], followed_by, path: Path | None = None) -> list[dict]:
    """Every promise of this call goes to the ledger with what followed it for its request (lane ids); owner is null
    when nothing did. Returns the unowned ones."""
    path = path or PROMISES
    rows = [{"t": round(time.time(), 3), "call": call, "did": p.get("did"), "wall": p["wall"], "promise": p["text"],
             "followed_by": followed_by(p), "owner": None} for p in made]
    for r in rows:
        r["owner"] = "lanes" if r["followed_by"] else None
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return [r for r in rows if r["owner"] is None]
