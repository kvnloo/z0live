"""Live check of topic_sessions on the real dashboard: two requests of one group land in ONE titled chat, the second
sees the first, and no bg_* session is made. Signs in with the dashboard's basic login from ~/.hermes/.env (never
printed), runs two short turns on `--profile` (default tek) in a probe chat, then archives the probe chat.

    .venv/bin/python live_topic_probe.py [--profile tek] [--keep]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
import topic_sessions as ts  # noqa: E402

BASE = "http://127.0.0.1:9119"


def env(name: str) -> str:
    for line in (Path.home() / ".hermes/.env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


async def token(http) -> str:
    async with http.post(BASE + "/auth/password-login", json={
            "provider": "basic", "username": env("HERMES_DASHBOARD_BASIC_AUTH_USERNAME"),
            "password": env("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD")}) as r:
        if r.status != 200:
            raise SystemExit(f"dashboard login: HTTP {r.status}")
        at = r.cookies.get("hermes_session_at")
        if not at:
            raise SystemExit("dashboard login: no session cookie")
        return at.value


def store_rows(profile: str, since: float):
    db = Path.home() / ".hermes/profiles" / profile / "state.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        return con.execute("SELECT id, title, message_count, source FROM sessions WHERE started_at >= ? AND "
                           "(title LIKE 'z0live · %' OR id LIKE 'bg\\_%' ESCAPE '\\') ORDER BY started_at",
                           (since,)).fetchall()


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="tek")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    stamp = time.strftime("%H%M%S")
    group = ts.Group("probe", f"probe {stamp} (live_topic_probe, archived after)", ("probe",),
                     "~/workspace/repos/z0live")
    began = time.time()
    out: dict[str, object] = {"title": group.title}
    async with aiohttp.ClientSession() as http:
        tok = await token(http)
        ws_url = BASE.replace("http", "ws") + "/api/ws"
        t = time.monotonic()
        a1, sid1, made1 = await asyncio.wait_for(ts.ask_topic(
            http, ws_url, tok, a.profile, group,
            lambda created: f"(probe) Remember the word PLUM{stamp}. Reply with exactly: TOPIC_OK_1"), 240)
        out["first"] = {"answer": a1[:80], "chat": sid1, "created": made1, "s": round(time.monotonic() - t, 1)}
        t = time.monotonic()
        a2, sid2, made2 = await asyncio.wait_for(ts.ask_topic(
            http, ws_url, tok, a.profile, group,
            lambda created: "(probe) Reply with exactly TOPIC_OK_2 followed by the word I asked you to remember, "
                            "without using any tools."), 240)
        out["second"] = {"answer": a2[:80], "chat": sid2, "created": made2, "s": round(time.monotonic() - t, 1)}
        rows = store_rows(a.profile, began - 1)
        out["store_rows_since_start"] = [list(r) for r in rows]
        out["same_chat"] = sid1 == sid2
        out["remembered"] = f"PLUM{stamp}" in a2
        out["new_bg_sessions"] = sum(1 for r in rows if str(r[0]).startswith("bg_"))
        if not a.keep:
            async with http.ws_connect(ws_url, params={"token": tok, "profile": a.profile},
                                       headers={"X-Hermes-Profile": a.profile}) as ws:
                await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.archive",
                                              "params": {"session_id": sid1, "profile": a.profile}}))
                reply = json.loads((await ws.receive()).data)
                out["archived"] = "error" not in reply
                out["archive_reply"] = reply.get("error") or reply.get("result")
    print(json.dumps(out, indent=1, default=str))
    ok = out["same_chat"] and out["first"]["created"] and not out["second"]["created"] and out["remembered"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
