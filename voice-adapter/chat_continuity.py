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
from pathlib import Path

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


RECENT = 20000  # only the newest message rows: a full-table scan took ~28 s and stalled call setup


def _key(text: str) -> str:
    return " ".join(text.split())[:80]


def find_chat(history, profile: str, db_path: Path | None = None) -> str:
    """Stored id of the newest chat holding any of the history's user messages, or ''."""
    keys = {_key(t) for t in user_texts(history) if len(_key(t)) >= 4}
    if not keys:
        return ""
    try:
        with sqlite3.connect(f"file:{db_path or _db(profile)}?mode=ro", uri=True, timeout=2) as db:
            rows = db.execute(
                "SELECT m.session_id, substr(m.content,1,400) FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE m.id > (SELECT max(id) FROM messages) - ? AND m.role='user' "
                "AND s.source NOT IN ('oneshot','tool','cron','kanban') ORDER BY m.id DESC", (RECENT,))
            sid = next((s for s, c in rows if isinstance(c, str) and _key(c) in keys), "")
            while sid:  # newest continuation: a GUI chat rolls over to a child at each handoff
                child = db.execute("SELECT id FROM sessions WHERE parent_session_id=? ORDER BY started_at DESC LIMIT 1",
                                   (sid,)).fetchone()
                if not child:
                    break
                sid = child[0]
            return sid
    except sqlite3.Error:
        return ""


async def ask_chat(http, token: str, profile: str, sid: str, request: str, timeout: float) -> str:
    """Submit one voice request into the chat and return its final assistant text."""
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
        live = (await rpc("session.resume", {"session_id": sid, "omit_messages": True, "inline_images": False,
                                             "lazy": True, **scope})).get("session_id") or sid
        await rpc("prompt.submit", {"session_id": live, "text": request, **scope})
        async for msg in ws:
            frame = json.loads(msg.data)
            p = frame.get("params") or {}
            if frame.get("method") == "event" and p.get("type") == "message.complete" and p.get("session_id") == live:
                text = (p.get("payload") or {}).get("text")
                text = text if isinstance(text, str) else json.dumps(text)
                if not text.strip():
                    raise RuntimeError("the chat turn ended without an answer")
                return text.strip()
        raise RuntimeError("chat socket closed before the turn finished")
