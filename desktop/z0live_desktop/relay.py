"""Relay a voice request into one of Kevin's Hermes sessions, and list recent sessions.

Read side: the clean profile's SessionDB, opened read-only. Write side: one message into the
target session through the gateway that owns it (`desktop/hermes_relay.py`, Hermes's own
viewer protocol: steer a running turn, otherwise queue the next turn). Same transport choice as
Dash's continuation seam (`bridge/src/hermes-continue.ts`): the owning gateway, never
`/v1/runs` and never a second CLI.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "desktop" / "hermes_relay.py"
# Interactive surfaces only: subagents, cron, kanban, one-shots and the voice's own API
# sessions are workers, not places Kevin talks.
HUMAN_SOURCES = ("local", "cli", "tui", "telegram", "discord", "slack", "signal", "whatsapp")
DRIVEN_FROM = ("cli", "tui")
MIN_MAIN_MESSAGES = 20  # below this a "local" row is a connectivity probe, not a work session
DEFAULT_WORDS = {"", "main", "default", "hermes", "my main session", "main session", "orchestrator"}
PREFIX = "Relayed from Kevin's z0live voice call"
RELAY_INTENT = re.compile(
    r"\b(?:pass (?:it|that|this|(?:a|the) (?:message|note))? ?along|relay|"
    r"(?:tell|ask|message|ping|notify|let) hermes|send (?:it|that|this|(?:a|the) (?:message|note)) "
    r"(?:over )?to hermes|(?:message|note) (?:to|for) hermes|let (?:the )?orchestrator know)\b",
    re.I,
)


def profile_home() -> Path:
    raw = os.environ.get("Z0LIVE_RELAY_HOME") or str(Path.home() / ".hermes/profiles/clean")
    return Path(raw).resolve()


def wants_relay(text: str) -> bool:
    return bool(RELAY_INTENT.search(text or ""))


@contextmanager
def _db(home: Path):
    db = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _one_line(value, limit=140) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _age(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _rows(db, limit, where="", args=()):
    marks = ",".join("?" * len(HUMAN_SOURCES))
    return db.execute(
        f"""SELECT id, source, created_source, title, message_count, cwd,
               COALESCE(last_activity_at, started_at) AS active, last_activity_description
            FROM sessions
            WHERE source IN ({marks}) AND parent_session_id IS NULL
              AND COALESCE(hidden,0)=0 AND COALESCE(archived,0)=0 {where}
            ORDER BY active DESC LIMIT ?""",
        (*HUMAN_SOURCES, *args, limit),
    ).fetchall()


def _last_user(db, sid) -> str:
    row = db.execute(
        # Kevin's own words: skip relays, steers and machine notices, which all start with "[".
        "SELECT content FROM messages WHERE session_id=? AND role='user' "
        "AND content NOT LIKE '[%' ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return _one_line(row["content"] if row else "", 120)


def _workers(db, sid, since) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE parent_session_id=? AND source='subagent' "
        "AND COALESCE(last_activity_at, started_at) >= ?",
        (sid, since),
    ).fetchone()
    return int(row["n"]) if row else 0


def describe(db, row, now) -> dict:
    title = row["title"] or "untitled session"
    workers = _workers(db, row["id"], now - 900)
    parts = [f"{title} ({row['source']}, active {_age(now - (row['active'] or now))})"]
    last = _last_user(db, row["id"])
    if last:
        parts.append(f"last asked: {last}")
    doing = _one_line(row["last_activity_description"], 90)
    if doing and now - (row["active"] or 0) < 900:
        parts.append(f"now: {doing}")
    if workers:
        parts.append(f"{workers} helper agent{'s' if workers != 1 else ''} active")
    return {"session_id": row["id"], "title": title, "source": row["source"],
            "last_active": row["active"], "summary": "; ".join(parts)}


def recent_sessions(limit=8, home=None, now=None, min_messages=4) -> list[dict]:
    """The last `limit` interactive Hermes sessions, newest first, each with a one-line summary."""
    home, now = home or profile_home(), now or time.time()
    limit = max(1, min(int(limit), 25))
    with _db(home) as db:
        rows = _rows(db, limit, "AND message_count >= ?", (int(min_messages),))
        return [describe(db, r, now) for r in rows]


def main_session(home=None):
    """Kevin's most recently active session that he drives from a terminal (CLI/TUI)."""
    pinned = os.environ.get("Z0LIVE_RELAY_DEFAULT", "").strip()
    home = home or profile_home()
    with _db(home) as db:
        if pinned:
            row = db.execute("SELECT id, title FROM sessions WHERE id=?", (pinned,)).fetchone()
            if row:
                return dict(row)
        marks = ",".join("?" * len(DRIVEN_FROM))
        rows = _rows(db, 1, f"AND created_source IN ({marks}) AND message_count >= ?",
                     (*DRIVEN_FROM, MIN_MAIN_MESSAGES))
        return {"id": rows[0]["id"], "title": rows[0]["title"] or "untitled session"} if rows else None


def resolve(to=None, home=None):
    """`to`: empty/"main" = the main session; a session id (or 8+ char prefix); or title words."""
    home = home or profile_home()
    query = " ".join(str(to or "").lower().split())
    if query in DEFAULT_WORDS:
        return main_session(home)
    with _db(home) as db:
        ident = str(to or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_\-]{8,}", ident):
            row = db.execute(
                "SELECT id, title FROM sessions WHERE id=? OR id LIKE ? ORDER BY "
                "COALESCE(last_activity_at, started_at) DESC LIMIT 1",
                (ident, ident + "%"),
            ).fetchone()
            if row:
                return {"id": row["id"], "title": row["title"] or "untitled session"}
        words = [w for w in re.findall(r"[a-z0-9]+", query) if w not in {"the", "a", "an", "session", "one"}]
        best, score = None, 0
        for row in _rows(db, 200):
            title = (row["title"] or "").lower()
            hits = sum(1 for w in words if w in title)
            if hits > score:
                best, score = row, hits
        if best is not None and (score == len(words) or (len(words) >= 3 and score * 3 >= 2 * len(words))):
            return {"id": best["id"], "title": best["title"] or "untitled session"}
    return None


def compose(text: str, when=None) -> str:
    stamp = time.strftime("%H:%M", time.localtime(when or time.time()))
    return f"[{PREFIX}, {stamp}] {text.strip()}"


def submission_id(target: str, text: str, key=None) -> str:
    seed = key or f"{target}\0{text}\0{int(time.time() // 60)}"
    return "z0live-relay-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def deliver(session_id: str, message: str, sub_id: str, home=None, runner=None) -> dict:
    """Hand one message to the owning gateway (`hermes_relay.py`); returns its JSON receipt."""
    home = home or profile_home()
    payload = json.dumps({"session_id": session_id, "text": message, "submission_id": sub_id})
    env = {**os.environ, "HERMES_HOME": str(home)}  # this child only; never exported
    run = runner or subprocess.run
    done = run([sys.executable or "python3", str(HELPER)], input=payload, capture_output=True, text=True, timeout=60, env=env)
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        return {"ok": False, "error": "relay_helper_failed", "detail": (done.stderr or "")[-300:]}
    return json.loads(lines[-1])


def relay(text: str, to=None, key=None, home=None, runner=None, store=None, sid=None) -> dict:
    """Send `text` into the resolved session. `say` is the exact read-back for the voice."""
    text = " ".join(str(text or "").split())
    if not text or len(text) > 4000:
        return {"ok": False, "error": "text must be 1-4000 characters"}
    target = resolve(to, home)
    if not target:
        options = [s["title"] for s in recent_sessions(5, home)]
        return {"ok": False, "error": "no_matching_session", "candidates": options,
                "say": "I couldn't tell which session you mean. Recent ones: " + "; ".join(options)}
    job_id = None
    if store is not None:
        store.work  # initialize the existing jobs receipt columns on fresh stores
        if not sid:
            raise ValueError("relay recording requires the originating voice session")
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise ValueError("unknown originating voice session")
            job_id = db.execute(
                "INSERT INTO jobs(kind,session_id,text,created,status,request) VALUES(?,?,?,?,?,?)",
                ("relay", sid, text, time.time(), "relay_pending",
                 json.dumps({"target_session_id": target["id"]})),
            ).lastrowid
    try:
        receipt = deliver(target["id"], compose(text), submission_id(target["id"], text, key), home, runner)
    except (OSError, ValueError, subprocess.SubprocessError):
        receipt = {"ok": False, "error": "gateway_delivery_unconfirmed"}
    if not receipt.get("ok"):
        error = receipt.get("error") or "gateway_refused"
        if error in {"TimeoutError", "ConnectionError", "BrokenPipeError", "OSError", "relay_helper_failed"}:
            receipt = {**receipt, "error": "gateway_delivery_unconfirmed", "cause": error}
        else:
            receipt = {**receipt, "error": error}
    if job_id is not None and store is not None:
        with store.connect() as db:
            db.execute("UPDATE jobs SET status=?,result=?,error=? WHERE id=?",
                       ("relay_accepted" if receipt.get("ok") else "failed", json.dumps(receipt),
                        receipt.get("error"), job_id))
    title = receipt.get("title") or target["title"]
    if not receipt.get("ok"):
        return {**receipt, "ok": False, "session_id": target["id"], "title": title,
                "say": (f"I couldn't confirm delivery to {title}; don't resend until it is checked."
                        if receipt.get("error") == "gateway_delivery_unconfirmed"
                        else f"I couldn't deliver that to {title}; nothing was sent.")}
    how = "into its running turn" if receipt.get("mode") == "steer" else "as its next message"
    return {**receipt, "session_id": target["id"], "title": title, "text": text,
            "say": f'Sent to {title}: "{text}"', "delivered": how}


def delivered_in_transcript(session_id: str, text: str, home=None) -> bool:
    """Whether the relayed text has landed in the target's stored transcript (a user turn or
    a steer inside a tool result)."""
    with _db(home or profile_home()) as db:
        row = db.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND content LIKE ? LIMIT 1",
            (session_id, "%" + text.strip()[:200] + "%"),
        ).fetchone()
    return row is not None
