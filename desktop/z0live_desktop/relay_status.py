"""Deterministic relay receipts and read-only target transcript observations.

Legacy delegation read-backs are discovery hints, NEVER delivery evidence. Only an
exact relay payload in the target transcript establishes delivered status.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path

from . import relay

STATUS_INTENT = re.compile(
    r"\b(?:did (?:it|that|my message) (?:go|get) through|did you send|"
    r"(?:see|check).*?(?:passed along|went through|what.*?(?:sent|send))|"
    r"what did hermes say|(?:relay|delivery) status|"
    r"(?:check|see).*?(?:bridge|backend)|no.*?not on my phone|"
    r"you need to use the backend)\b", re.I,
)
LEGACY = re.compile(r'^Sent to (.+?): "(.*)"$', re.S)


def wants_status(text):
    return bool(STATUS_INTENT.search(text or ""))


def _json(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def status(broker_path, device=None, limit=5, home=None):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20")
    records, gaps = [], []
    with closing(sqlite3.connect(Path(broker_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        scope = " AND s.device=?" if device is not None else ""
        args = (device,) if device is not None else ()
        for row in db.execute(
            "SELECT j.* FROM jobs j JOIN sessions s ON s.id=j.session_id "
            "WHERE j.kind='relay'" + scope + " ORDER BY j.created DESC LIMIT 100", args,
        ):
            payload, receipt = _json(row["request"]), _json(row["result"])
            records.append({"id": "job:" + str(row["id"]), "created": row["created"],
                            "text": row["text"], "session_id": payload.get("target_session_id"),
                            "title": receipt.get("title"), "receipt": receipt,
                            "error": row["error"], "legacy": False})
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_delegations'").fetchone():
            for row in db.execute(
                "SELECT d.* FROM live_delegations d JOIN sessions s ON s.id=d.sid "
                "WHERE d.route='hermes' AND d.final LIKE 'Sent to %'" + scope +
                " ORDER BY d.created DESC LIMIT 100", args,
            ):
                match = LEGACY.fullmatch(row["final"] or "")
                if match:
                    records.append({"id": "delegation:" + row["id"], "created": row["created"],
                                    "text": match[2], "title": match[1], "session_id": None,
                                    "receipt": {}, "error": None, "legacy": True})
    records.sort(key=lambda r: r["created"], reverse=True)
    items, sources = [], []
    try:
        with relay._db(home or relay.profile_home()) as db:
            for record in records:
                item = _observe(db, record)
                # New jobs and legacy final read-backs can describe the same relay.
                if any(i["target_session_id"] == item["target_session_id"] and
                       i["text"] == item["text"] and abs(i["created"] - item["created"]) < 120
                       and legacy != record["legacy"] for i, legacy in zip(items, sources)):
                    continue
                items.append(item)
                sources.append(record["legacy"])
                if len(items) == limit:
                    break
    except (OSError, sqlite3.Error):
        gaps.append("target_state_unavailable")
        items = [_unavailable(r) for r in records[:limit]]
    return {"relays": items, "gaps": gaps,
            "reply_note": "Assistant activity after the relay is not proof it answers that relay."}


def _unavailable(record):
    receipt = record["receipt"]
    return {"id": record["id"], "created": record["created"], "text": record["text"],
            "target_session_id": record["session_id"], "current_title": None,
            "latest_topic": None, "delivery_state": "failed" if record["error"] and record["error"] != "gateway_delivery_unconfirmed" else
            "accepted by gateway" if receipt.get("ok") else "unknown",
            "error": record["error"], "replied_since": None, "reply_excerpt": None,
            "reply_at": None, "evidence": "gateway_receipt" if receipt else
            "legacy_readback_only" if record["legacy"] else "submission_pending"}


def _observe(db, record):
    item = _unavailable(record)
    # instr, not LIKE: punctuation, percent and underscore in the exact text stay literal.
    where = " AND session_id=?" if record["session_id"] else ""
    # New jobs are checkpointed BEFORE gateway submission; an older identical relay
    # cannot prove this attempt landed. Legacy merged delegations can be recorded later.
    since = record["created"] - (120 if record["legacy"] else 0)
    args = (record["text"], relay.PREFIX, since)
    if record["session_id"]:
        args += (record["session_id"],)
    # A frozen title is only a search hint. Verify exact transcript evidence and fall
    # back to all targets if it has since changed; never derive liveness from the title.
    hint = None
    if not record["session_id"]:
        hints = db.execute("SELECT id FROM sessions WHERE title=? LIMIT 2", (record["title"],)).fetchall()
        hint = hints[0]["id"] if len(hints) == 1 else None
    query = ("SELECT session_id,timestamp,content FROM messages WHERE instr(content,?)>0 "
             "AND instr(content,?)>0 AND timestamp>=? AND role IN ('user','tool')")
    order = " ORDER BY timestamp LIMIT 100"
    candidates = db.execute(query + where + (" AND session_id=?" if hint else "") + order,
                            args + ((hint,) if hint else ())).fetchall()
    if not candidates and hint:
        candidates = db.execute(query + order, args).fetchall()
    # Require the exact contiguous relay wrapper, not quoted summaries / assistant guesses.
    pattern = re.compile(r"\[" + re.escape(relay.PREFIX) + r", \d{2}:\d{2}\] " +
                         re.escape(record["text"]) + r"(?=$|\n)")
    matches = [row for row in candidates if pattern.search(row["content"]) and
               not row["content"].startswith("[CONTEXT COMPACTION")]
    landed = matches[0] if len({r["session_id"] for r in matches}) == 1 else None
    sid = landed["session_id"] if landed else record["session_id"]
    item["target_session_id"] = sid
    if landed:
        item.update(delivery_state="delivered", error=None, evidence="target_transcript")
    if not sid:
        return item
    target = db.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
    item["current_title"] = target["title"] if target else None
    item["latest_topic"] = relay._last_user(db, sid)
    after = max(record["created"], landed["timestamp"] if landed else record["created"])
    reply = db.execute(
        "SELECT content,timestamp FROM messages WHERE session_id=? AND role='assistant' "
        "AND timestamp>? AND length(trim(content))>0 ORDER BY timestamp DESC,id DESC LIMIT 1",
        (sid, after),
    ).fetchone()
    item.update(replied_since=bool(reply), reply_excerpt=relay._one_line(reply["content"], 400)
                if reply else None, reply_at=reply["timestamp"] if reply else None)
    return item


def spoken(result):
    if not result["relays"]:
        return "I found no recorded relays."
    item = result["relays"][0]
    target = (item["target_session_id"] or "unverified")[:16]
    first = f'The last relay is {item["delivery_state"]} to session {target}.'
    if item["reply_excerpt"]:
        excerpt = re.split(r"(?<=[.!?])\s", item["reply_excerpt"], maxsplit=1)[0]
        return first + ' Later assistant activity (not necessarily a reply to it): ' + excerpt
    return first + (" I could not read its replies." if item["replied_since"] is None
                    else " No assistant reply has been recorded since then.")
