"""Shared context (blackboard) for one voice session: lanes write, the voice reads by tool call.

Entries are versioned: author lane, role, confidence, epoch, supersedes, status
draft|verified|corrected. Any lane may write a draft; only verifier roles (large, council) may
verify or correct one. A correction is a new entry that supersedes the old one, so history is kept
and `read` returns only the current version of each fact. Epochs are the P1b kill switch: barge-in
or "never mind" bumps the session epoch, writes for an older epoch are refused, and `read` shows the
current epoch only (LANES.md splice rules). SQLite in broker.db is canonical.
"""
from __future__ import annotations

import json
import time

ROLES = {"fast", "small", "large", "council", "voice"}
VERIFIERS = {"large", "council"}
KINDS = {"intent", "answer", "fact", "note", "spec"}  # spec: a speculative answer from a partial (lanes only)


class Stale(ValueError):
    """A write for an epoch the session has already cancelled."""


class Blackboard:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS lane_board(id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL,
              epoch INTEGER NOT NULL, turn TEXT, lane TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
              text TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL, supersedes INTEGER,
              superseded_by INTEGER, sources TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS lane_board_sid ON lane_board(sid, epoch);
            CREATE TABLE IF NOT EXISTS lane_epoch(sid TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
              reason TEXT, updated REAL NOT NULL);
            """)

    def epoch(self, sid) -> int:
        with self.store.connect() as db:
            row = db.execute("SELECT epoch FROM lane_epoch WHERE sid=?", (sid,)).fetchone()
        return row[0] if row else 0

    def bump(self, sid, reason) -> int:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT epoch FROM lane_epoch WHERE sid=?", (sid,)).fetchone()
            value = (row[0] if row else 0) + 1
            db.execute("INSERT OR REPLACE INTO lane_epoch VALUES(?,?,?,?)", (sid, value, reason, time.time()))
        return value

    def write(self, sid, lane, role, text, confidence, epoch, kind="answer", turn=None, sources=(),
              supersedes=None, status="draft") -> int:
        if role not in ROLES or kind not in KINDS:
            raise ValueError("unknown role or kind")
        if status != "draft" and role not in VERIFIERS:
            raise PermissionError(f"only verifier lanes may write {status} entries")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("entry text must be 1..4000 characters")
        confidence = max(0.0, min(1.0, float(confidence)))
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT epoch FROM lane_epoch WHERE sid=?", (sid,)).fetchone()
            if epoch < (row[0] if row else 0):
                raise Stale(f"epoch {epoch} was cancelled")
            cur = db.execute(
                "INSERT INTO lane_board(sid,epoch,turn,lane,role,kind,text,confidence,status,supersedes,"
                "sources,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, epoch, turn, lane, role, kind, text.strip(), confidence, status, supersedes,
                 json.dumps(list(sources)), now, now))
            if supersedes is not None:
                db.execute("UPDATE lane_board SET superseded_by=?,updated=? WHERE id=? AND sid=?",
                           (cur.lastrowid, now, supersedes, sid))
            return cur.lastrowid

    def entry(self, entry_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM lane_board WHERE id=?", (entry_id,)).fetchone()
        return self._row(row) if row else None

    def verify(self, entry_id, lane, role) -> bool:
        if role not in VERIFIERS:
            raise PermissionError("only verifier lanes may verify")
        with self.store.connect() as db:
            cur = db.execute(
                "UPDATE lane_board SET status='verified',updated=?,sources=json_insert(sources,'$[#]',?) "
                "WHERE id=? AND status='draft' AND superseded_by IS NULL",
                (time.time(), "verified-by:" + lane, entry_id))
        return bool(cur.rowcount)

    def correct(self, entry_id, lane, role, text, confidence, sources=()) -> int:
        """A verifier replaces an entry: new `corrected` version, same epoch/turn, old one superseded."""
        if role not in VERIFIERS:
            raise PermissionError("only verifier lanes may correct")
        old = self.entry(entry_id)
        if not old or old["superseded_by"] is not None:
            raise LookupError("no current entry to correct")
        return self.write(old["sid"], lane, role, text, confidence, old["epoch"], kind=old["kind"],
                          turn=old["turn"], sources=sources, supersedes=entry_id, status="corrected")

    def read(self, sid, limit=12) -> dict:
        """What the voice sees: current-epoch entries, newest last, superseded versions hidden."""
        epoch = self.epoch(sid)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM lane_board WHERE sid=? AND epoch=? AND superseded_by IS NULL "
                "ORDER BY id DESC LIMIT ?", (sid, epoch, limit)).fetchall()
        return {"epoch": epoch, "entries": [self._row(r) for r in reversed(rows)]}

    @staticmethod
    def _row(row):
        value = dict(row)
        value["sources"] = json.loads(value["sources"])
        return value
