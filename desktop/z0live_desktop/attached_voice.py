"""Immutable exact attachment of a Dash voice call to its owner-selected Hermes session."""
import json
import re
from pathlib import Path
import sqlite3
import shlex


def target_home(profile):
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
        raise ValueError("invalid exact Hermes profile")
    return Path.home() / ".hermes" if profile == "default" else Path.home() / ".hermes" / "profiles" / profile


def validate_target(target):
    if not isinstance(target, dict) or set(target) != {"profile", "session"}:
        raise ValueError("Dash voice requires an exact attached Hermes target")
    home = target_home(target["profile"])
    sid = target["session"]
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", sid):
        raise ValueError("invalid exact Hermes session")
    try:
        with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute("SELECT id FROM sessions WHERE id=? AND COALESCE(hidden,0)=0 AND COALESCE(archived,0)=0", (sid,)).fetchone()
        if not row:
            raise ValueError("attached Hermes session not found")
    except sqlite3.Error as exc:
        raise ValueError("attached Hermes profile unavailable") from exc
    return dict(target)


def bind(db, sid, target):
    db.execute("CREATE TABLE IF NOT EXISTS dash_voice_targets(sid TEXT PRIMARY KEY, target TEXT NOT NULL)")
    db.execute("INSERT INTO dash_voice_targets(sid,target) VALUES(?,?)", (sid, json.dumps(target)))


def target_for(store, sid):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dash_voice_targets'").fetchone():
            return None
        row = db.execute("SELECT target FROM dash_voice_targets WHERE sid=?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def attached_instructions(store, sid, delegation_id):
    target = target_for(store, sid)
    if not target:
        return ""
    # Binding is in SQLite, never mutable app_state/context and never a most-recent heuristic.
    return ("\nDASH ATTACHED CONVERSATION OVERRIDE: Every request from this call belongs to the exact attached Hermes conversation. "
            "Do not implement it in this backend or select another session. Preserve the owner's original words and concrete details. "
            f"Run Z0LIVE_DATA={shlex.quote(str(store.directory))} {Path(__file__).resolve().parents[2]}/bin/z0live relay --session {sid} --key dash:{sid}:{delegation_id} --wait 180 \"MESSAGE\". "
            "The relay command obtains the immutable profile/session binding internally; never add --to. "
            "An accepted/queued receipt is not an answer: speak only its final_reply when reply_state is completed. "
            "For reply_state pending/unknown return an empty final response, no waiting/status narration. "
            "If delivery failed, state the actual blocker briefly. Never claim the attached Hermes replied without final_reply.")
