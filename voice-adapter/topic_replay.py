"""Replay a day's calls through topic_sessions: how many Hermes sessions the calls' requests made, and how many the
group chats would make. Read-only (voice traces + the profiles' state.db in mode=ro).

    python topic_replay.py 2026-11-28 --from 13:00 --to 14:00 [--profiles tek,pc]

Before: every delegation.background in the traces (one bg_* session each), plus the api_server sessions the guest
leg (z0live-guest-*) and ask @bot (z0live-ask-*) opened in the window. After: the primary leg's requests routed by
topic_sessions.Router at their recorded times, a request running from delegation.start to delegation.done (an
unfinished one until the 90 s cap), so a burst past DEPTH counts as a background session as it would run; the
guest/ask requests keyed by topic_sessions.day_session.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import topic_sessions as ts  # noqa: E402

TRACES = Path.home() / ".local/share/z0live/voice-traces"
CAP = 90.0


def primary(day: str, t0: str, t1: str, depth: int):
    groups = ts.load_groups()
    before, chats, overflow, rows = 0, set(), 0, []
    for f in sorted((TRACES / day).glob("*.jsonl")):
        if not (t0.replace(":", "") <= f.name[:4] < t1.replace(":", "")):
            continue
        evs = [json.loads(line) for line in f.open() if line.strip()]
        if not any(e.get("ev") in ("delegation.background", "delegation.chat") for e in evs):
            continue  # no request reached the dashboard (unit-test and probe traces use the host path): no sessions
        starts = [e for e in evs if e.get("ev") == "delegation.start"]
        done = {e.get("did"): e["t"] for e in evs if e.get("ev") == "delegation.done"}
        before += sum(1 for e in evs if e.get("ev") == "delegation.background")
        router = ts.Router(groups=groups)
        running: list[tuple[float, str]] = []  # (ends at, group)
        for e in starts:
            running = [(end, k) for end, k in running if end > e["t"]]
            router.inflight = Counter(k for _, k in running)
            group, why = router.route(e.get("request") or "", now=e["t"])
            if router.inflight.get(group.key, 0) >= depth:
                overflow += 1
                where = "beside (bg_*)"
            else:
                chats.add(group.key)
                where = ts.TITLE_PREFIX + group.label
                running.append((done.get(e["did"], e["t"] + CAP), group.key))
            rows.append((f.name[:6], round(e["t"], 1), group.key, why, where, (e.get("request") or "")[:70]))
    return before, len(chats) + overflow, chats, overflow, rows


def api_sessions(profiles, day: str, t0: str, t1: str):
    lo = dt.datetime.fromisoformat(f"{day}T{t0}").timestamp()
    hi = dt.datetime.fromisoformat(f"{day}T{t1}").timestamp()
    groups = ts.load_groups()
    before, after, rows = 0, set(), []
    for profile in profiles:
        db = Path.home() / ".hermes" / "profiles" / profile / "state.db"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            for sid, started in con.execute(
                    "SELECT id, started_at FROM sessions WHERE (id LIKE 'z0live-guest-%' OR id LIKE 'z0live-ask-%') "
                    "AND started_at BETWEEN ? AND ? ORDER BY started_at", (lo, hi)):
                first = con.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id "
                                    "LIMIT 1", (sid,)).fetchone()
                text = (first[0] if first else "") or ""
                group, _ = ts.classify(text, groups)
                prefix = "z0live-guest" if sid.startswith("z0live-guest-") else "z0live-ask-" + sid.split("-")[2]
                new = ts.day_session(prefix, group, started)
                before += 1
                after.add((profile, new))
                rows.append((profile, dt.datetime.fromtimestamp(started).strftime("%H:%M:%S"), sid[:44], new,
                             text[:50]))
    return before, len(after), rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--from", dest="t0", default="00:00")
    ap.add_argument("--to", dest="t1", default="23:59")
    ap.add_argument("--profiles", default="tek,pc")
    ap.add_argument("--depth", type=int, default=ts.DEPTH)
    ap.add_argument("--rows", action="store_true")
    a = ap.parse_args(argv)
    pb, pa, chats, over, prow = primary(a.day, a.t0, a.t1, a.depth)
    gb, ga, grow = api_sessions(a.profiles.split(","), a.day, a.t0, a.t1)
    if a.rows:
        for r in prow:
            print("primary", *r, sep=" | ")
        for r in grow:
            print("guest/ask", *r, sep=" | ")
    print(json.dumps({"window": f"{a.day} {a.t0}-{a.t1}", "depth": a.depth,
                      "primary": {"before_sessions": pb, "after_sessions": pa, "group_chats": sorted(chats),
                                  "overflow_background": over},
                      "guest_and_ask": {"before_sessions": gb, "after_sessions": ga},
                      "total": {"before": pb + gb, "after": pa + ga}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
