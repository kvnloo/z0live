"""Voice turns land in the chat the owner started Live Voice from, not a hidden oneshot session.

The app sends that chat's recent history with the call but no session id, so the chat is found by
its latest user message in the profile's state.db (then its newest continuation, since GUI chats roll
over to child sessions). Each delegated request is then submitted to that chat through the
dashboard's own /api/ws, authenticated with the app's own bearer (`?token=`, the same verify_session
seam as its REST calls), so it shows in the app like a typed message, with its tool summaries.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import closing
import time
from pathlib import Path

import owner

WS = "ws://127.0.0.1:9119/api/ws"


def _db(profile: str) -> Path:
    home = Path.home() / ".hermes"
    return (home / "profiles" / profile if profile else home) / "state.db"


def user_texts(history) -> list[str]:
    out = []
    for item in history if isinstance(history, list) else []:
        if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), list):
            text = " ".join(c.get("text", "") for c in item["content"]
                            if isinstance(c, dict) and isinstance(c.get("text"), str)).strip()
            if text:
                out.append(text)
    return out


NOT_A_CHAT = "('oneshot','tool','cron','kanban','subagent')"
# Never the owner's chat: local sessions, side runs (bg_*) and z0live's own api_server runs (z0live-ask-*, probes).
NOT_OWNER_CHATS = "id NOT LIKE 'local-%' AND id NOT LIKE 'bg\\_%' ESCAPE '\\' AND id NOT LIKE 'z0live-%'"
RECENT = 20000  # only the newest message rows: a full-table scan took ~28 s and stalled call setup


def _key(text: str) -> str:
    return " ".join(text.split())[:80]


def find_chat(history, profile: str, db_path: Path | None = None) -> str:
    """Stored id of the newest chat holding any of the history's user messages, or ''."""
    keys = {_key(t) for t in user_texts(history) if len(_key(t)) >= 4}
    if not keys:
        return ""
    try:
        with closing(sqlite3.connect(f"file:{db_path or _db(profile)}?mode=ro", uri=True, timeout=2)) as db:
            rows = db.execute(
                "SELECT m.session_id, substr(m.content,1,400) FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE m.id > (SELECT max(id) FROM messages) - ? AND m.role='user' "
                f"AND s.source NOT IN {NOT_A_CHAT} ORDER BY m.id DESC", (RECENT,))
            sid = next((s for s, c in rows if isinstance(c, str) and _key(c) in keys), "")
            if not sid:  # 11-27: two days of lanes pushed a live chat's turns ~80k rows back; search recent chats instead
                rows = db.execute(
                    "SELECT m.session_id, substr(m.content,1,400) FROM messages m WHERE m.role='user' AND m.session_id IN "
                    f"(SELECT id FROM sessions WHERE source NOT IN {NOT_A_CHAT} AND id NOT LIKE 'local-%' "
                    "ORDER BY started_at DESC LIMIT 80) ORDER BY m.id DESC")
                sid = next((s for s, c in rows if isinstance(c, str) and _key(c) in keys), "")
            while sid:  # newest continuation: a GUI chat rolls over to a child at each handoff
                # a subagent is also a child of the chat, but it is the chat's helper, never its continuation
                child = db.execute(f"SELECT id FROM sessions WHERE parent_session_id=? AND source NOT IN {NOT_A_CHAT} "
                                   "ORDER BY started_at DESC LIMIT 1", (sid,)).fetchone()
                if not child:
                    break
                sid = child[0]
            return sid
    except sqlite3.Error:
        return ""


def newest_chat(profile: str, db_path: Path | None = None) -> str:
    """Stored id of the profile's newest chat (its newest continuation), or ''. A background agent only needs a chat
    to run beside, so a call whose history matched no chat still gets the fast path instead of a cold CLI run
    (11-27 11:20: three CLI runs, none answered before hang-up). Never a side run (bg_*) or a local session."""
    try:
        with closing(sqlite3.connect(f"file:{db_path or _db(profile)}?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute(f"SELECT id FROM sessions WHERE source NOT IN {NOT_A_CHAT} AND {NOT_OWNER_CHATS} "
                             "ORDER BY started_at DESC LIMIT 1").fetchone()
            return row[0] if row else ""
    except sqlite3.Error:
        return ""


PIN = Path.home() / ".config" / "z0live" / "anchor-session"
ANCHOR_WINDOW_S = 6 * 3600  # only chats the owner typed in during the last 6 h compete
DECAY_PER_HOUR = 2.0  # score = log2(1 + chat size) - 2 per hour since the owner's last message in it


def _pinned(db, path: Path) -> str:
    """The session id in the pin file (first non-comment line), if that session is in this profile's store."""
    try:
        line = next((l.strip() for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")),
                    "")
    except OSError:
        return ""
    sid = line.split()[0] if line else ""
    if sid and db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        return sid
    return ""


def _chain_size(db, sid: str, size: int) -> int:
    """Messages in the chat and the sessions it continues (a GUI chat rolls over to a child at each handoff, so a
    fresh continuation of a 500-message chat is still a 500-message chat)."""
    seen = {sid}
    parent = db.execute("SELECT parent_session_id FROM sessions WHERE id=?", (sid,)).fetchone()
    while parent and parent[0] and parent[0] not in seen and len(seen) < 20:
        seen.add(parent[0])
        row = db.execute("SELECT message_count, parent_session_id FROM sessions WHERE id=?", (parent[0],)).fetchone()
        if not row:
            break
        size += row[0] or 0
        parent = (row[1],)
    return size


def _continuation(db, sid: str) -> str:
    while sid:  # newest continuation; a subagent child is the chat's helper, never its continuation
        child = db.execute(f"SELECT id FROM sessions WHERE parent_session_id=? AND source NOT IN {NOT_A_CHAT} "
                           "ORDER BY started_at DESC LIMIT 1", (sid,)).fetchone()
        if not child:
            break
        sid = child[0]
    return sid


def rank_chats(db, now: float, window: float = ANCHOR_WINDOW_S) -> list[tuple[float, str, float, int]]:
    """The owner's chats with a user message in the window, best first: (score, sid, last user message ts, size)."""
    import math
    rows = db.execute(f"SELECT id, message_count FROM sessions WHERE source NOT IN {NOT_A_CHAT} AND {NOT_OWNER_CHATS} "
                      "AND (coalesce(last_activity_at, started_at) > ? OR started_at > ?) "
                      "ORDER BY coalesce(last_activity_at, started_at) DESC LIMIT 200",
                      (now - window, now - 30 * 86400)).fetchall()
    ranked = []
    for sid, count in rows:
        # indexed (session_id, timestamp): one short range read per chat
        last = db.execute("SELECT max(timestamp) FROM messages WHERE session_id=? AND role='user' AND timestamp>? "
                          "AND timestamp<=?", (sid, now - window, now)).fetchone()[0]
        if not last:
            continue
        size = _chain_size(db, sid, count or 0)
        score = math.log2(1 + size) - DECAY_PER_HOUR * max(0.0, now - last) / 3600
        ranked.append((score, sid, last, size))
    ranked.sort(reverse=True)
    return ranked


def anchor_chat(profile: str, db_path: Path | None = None, now: float | None = None,
                pin: Path | None = None) -> tuple[str, str]:
    """(stored id, why) of the chat a call's background agents run beside when the call matched no chat.

    11-28 10:15: newest_chat picked a 22-message side chat started a minute earlier, so the call's memory was 170
    chars instead of the ~40k of the 559-message chat the owner had typed in one minute before that. Now: a pin file
    (~/.config/z0live/anchor-session holding a session id) wins; else the chat with the owner's most recent message in
    the last 6 h, weighted by size (log2 of its messages, -2 per hour of age), so a big working chat beats a tiny
    side chat a few minutes newer (each doubling of size is worth 30 min of recency: 559 vs 22 messages = 2.3 h).
    Else the newest chat."""
    import time
    now = time.time() if now is None else now
    try:
        with closing(sqlite3.connect(f"file:{db_path or _db(profile)}?mode=ro", uri=True, timeout=2)) as db:
            sid = _pinned(db, PIN if pin is None else pin)
            if sid:
                return _continuation(db, sid), "pin"
            ranked = rank_chats(db, now)
            if ranked:
                return _continuation(db, ranked[0][1]), "recent_user"
    except sqlite3.Error:
        return "", "error"
    sid = newest_chat(profile, db_path)
    return sid, "newest" if sid else "none"


SUMMARY_MARK = "CONTEXT COMPACTION"


def chat_memory(profile: str, sid: str, db_path: Path | None = None, summary_cap: int = 24000,
                tail_cap: int = 16000) -> str:
    """What a background agent beside chat `sid` needs to know about it: the chat's newest compaction summary (the
    whole project state, ~29k chars on 11-27) and the owner's and the assistant's turns since, newest kept first. A
    background agent does not see the chat (ask_background), and the 6,000-char tail it had was two status replies,
    so the call lacked useful context. Tool rows and runtime envelopes
    ([IMPORTANT: ..., [Context from ...) are skipped. '' when there is no chat or no store."""
    if not sid:
        return ""
    try:
        with closing(sqlite3.connect(f"file:{db_path or _db(profile)}?mode=ro", uri=True, timeout=2)) as db:
            # Only a real compaction row: it STARTS with the marker (or the [PRIOR CONTEXT wrapper around it) and is
            # a user/assistant/system row. A tool row or a reply that merely quotes the marker is not one (11-27: a
            # patch diff holding test text was picked as "the summary").
            row = db.execute("SELECT id, content FROM messages WHERE session_id=? "
                             "AND role IN ('user','assistant','system') "
                             "AND (content LIKE ? OR content LIKE '[PRIOR CONTEXT%') AND content LIKE ? "
                             "ORDER BY id DESC LIMIT 1",
                             (sid, f"[{SUMMARY_MARK}%", f"%[{SUMMARY_MARK}%")).fetchone()
            since, summary = (row[0], row[1]) if row else (0, "")
            q = ("SELECT role, content{} FROM messages WHERE session_id=? AND id>? "
                 "AND role IN ('user','assistant') AND length(content)>0 ORDER BY id DESC LIMIT 400")
            try:
                turns = db.execute(q.format(", timestamp"), (sid, since)).fetchall()
            except sqlite3.OperationalError:  # a store without the timestamp column (old schema, test fixtures)
                turns = db.execute(q.format(""), (sid, since)).fetchall()
    except sqlite3.Error:
        return ""
    if summary:
        cut = summary.find(f"[{SUMMARY_MARK}")
        summary = summary[cut:] if cut >= 0 else summary
        # Drop the compaction preamble ("Do NOT answer questions ... do not call tools, wait for a new user message"):
        # it is addressed to the chat's next turn, and a background agent reading it may decline the owner's request.
        body = summary.find("\n## ")
        if body >= 0:
            summary = summary[body + 1:]
        if len(summary) > summary_cap:
            summary = summary[:summary_cap] + "\n[... summary cut ...]"
    tail, used = [], 0
    for role, content, *stamp in turns:
        text = content.strip()
        if not text or (text.startswith("[") and not text.startswith("[OUT-OF-BAND USER MESSAGE")):
            continue
        # Each turn carries its clock time, so a reader can tell a 3-hour-old setup note from now (11-28 13:55: the
        # call repeated a morning note that groot did the TV installs after the TV had been moved to the Studio).
        at = _clock(stamp[0]) if stamp else ""
        line = f"{at}{owner.LABEL if role == 'user' else 'Assistant'}: {text[:3000]}"
        if used + len(line) > tail_cap:
            break
        tail.append(line)
        used += len(line)
    parts = []
    if summary:
        parts.append(HISTORY_NOTE + "\n\nSummary of this chat so far (written earlier; may be out of date):\n" + summary)
    if tail:
        parts.append(("" if summary else HISTORY_NOTE + "\n\n") + "Turns since, oldest first (clock time first):\n"
                     + "\n\n".join(reversed(tail)))
    return "\n\n".join(parts)


# The chat is history, not the state of the machines (11-28 13:55, the owner: "WHY WOULD I WANT A LIVE CALL TO READ ME
# STALE FUCKING INFORMATION"). Facts about devices, installs, addresses, services, jobs and builds change within the
# hour; the reader must check them live before saying them.
HISTORY_NOTE = ("This is past conversation, not live state. Anything below about a device, install, IP address, "
                "service, running job, build or setting may have changed since it was written. Before you state any "
                "such fact, check it live with a tool now (command output, file, API) and answer from that; if you "
                "did not check it, say you have not checked yet. When the chat and a live check disagree, the live "
                "check wins and you say what changed.")


def _clock(ts) -> str:
    """'[13:41] ' local time of a message timestamp; '' when it is missing or bad."""
    try:
        return time.strftime("[%H:%M] ", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class NotStarted(RuntimeError):
    """The dashboard refused a background run before it began (no task id): nothing runs, so another path may."""


class ChatBusy(RuntimeError):
    """The chat's own turn is running (the call's, or one the owner typed): a prompt submitted now would fold into it."""


JOINED = frozenset({"steered", "redirected"})  # prompt.submit receipts for a prompt folded into the running turn


async def _socket(http, token: str, profile: str, sid: str):
    """An /api/ws socket resumed onto the chat: (ws, rpc, live sid, scope)."""
    ids = itertools.count(1)
    headers = {"X-Hermes-Profile": profile} if profile else {}
    params = {"token": token, **({"profile": profile} if profile else {})}
    ws = await http.ws_connect(WS, params=params, headers=headers, heartbeat=30, max_msg_size=0)

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

    scope = {"profile": profile} if profile else {}  # the dashboard picks the store per RPC, not per socket
    try:
        live = (await rpc("session.resume", {"session_id": sid, "omit_messages": True, "inline_images": False,
                                             "lazy": True, **scope})).get("session_id") or sid
    except BaseException:
        await ws.close()
        raise
    return ws, rpc, live, scope


async def ask_background(http, token: str, profile: str, sid: str, request: str, on_receipt=None) -> str:
    """Run one voice request as a Hermes background agent beside the chat's running turn (the dashboard's
    prompt.background, the app's /background): the RPC returns a task id at once, and the answer arrives as
    background.complete {task_id, text} on the chat's sockets. Same profile, tools and approval rules as a chat
    turn; it does not see the chat's history, so the caller puts the call's recent words in the request."""
    try:
        ws, rpc, live, scope = await _socket(http, token, profile, sid)
    except RuntimeError as exc:  # session.resume refused: nothing started
        raise NotStarted(str(exc)) from exc
    try:
        try:
            task_id = (await rpc("prompt.background", {"session_id": live, "text": request, **scope})).get("task_id")
        except RuntimeError as exc:  # e.g. 5032 agent still starting: the RPC errored, no task began
            raise NotStarted(str(exc)) from exc
        if not task_id:
            raise NotStarted("prompt.background returned no task id")
        if on_receipt:
            on_receipt(task_id)
        async for msg in ws:
            frame = json.loads(msg.data)
            p = frame.get("params") or {}
            payload = p.get("payload") or {}
            if frame.get("method") == "event" and p.get("type") == "background.complete" \
                    and payload.get("task_id") == task_id:
                text = str(payload.get("text") or "").strip()
                if not text or text.startswith("error:"):
                    raise RuntimeError(f"background agent failed: {text[:200] or 'no answer'}")
                return text
        raise RuntimeError("chat socket closed before the background agent finished")
    finally:
        await ws.close()


async def ask_chat(http, token: str, profile: str, sid: str, request: str, timeout: float,
                   only_if_idle: bool = False) -> str | None:
    """Submit one voice request into the chat and return the final text of the turn it started, or None when the
    chat was busy and the request joined the running turn: that turn's one final answers both, and it already
    belongs to the request that started it. A queued request waits for its own turn, after the running one."""
    ids = itertools.count(1)
    headers = {"X-Hermes-Profile": profile} if profile else {}
    params = {"token": token, **({"profile": profile} if profile else {})}
    async with http.ws_connect(WS, params=params, headers=headers, heartbeat=30, max_msg_size=0) as ws:
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

        scope = {"profile": profile} if profile else {}  # the dashboard picks the store per RPC, not per socket
        resumed = await rpc("session.resume", {"session_id": sid, "omit_messages": True, "inline_images": False,
                                               "lazy": True, **scope})
        live = resumed.get("session_id") or sid
        if only_if_idle and resumed.get("running"):  # a live chat reports its running turn (session_registry.py)
            raise ChatBusy(f"chat {sid} has a turn running")
        status = (await rpc("prompt.submit", {"session_id": live, "text": request, **scope})).get("status")
        if status in JOINED:
            return None
        started = status != "queued"  # a queued prompt's turn begins after the running turn's message.complete
        async for msg in ws:
            frame = json.loads(msg.data)
            p = frame.get("params") or {}
            if frame.get("method") != "event" or p.get("session_id") != live:
                continue
            if p.get("type") == "message.start":
                started = True
            elif p.get("type") == "message.complete" and started:
                text = (p.get("payload") or {}).get("text")
                text = text if isinstance(text, str) else json.dumps(text)
                if not text.strip():
                    raise RuntimeError("the chat turn ended without an answer")
                return text.strip()
        raise RuntimeError("chat socket closed before the turn finished")
