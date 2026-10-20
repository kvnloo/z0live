#!/usr/bin/env python3
"""Opt-in real voice-work test: ask_z0 -> persistent Hermes backend -> garden_room equipment CLI.

Run against an ISOLATED broker (desktop/tests/live_isolated_broker.py) started with
Z0LIVE_GARDEN_ROOT pointing at a disposable garden_room checkout, so the delegated agent writes
only that checkout's private data. This is the text path the phone's realtime model uses after it
calls ask_z0; it does not exercise audio. One real native model run (subscription/unknown cost).

  Z0LIVE_DATA=/private/qual Z0LIVE_TEST_BASE=http://127.0.0.1:18792 GARDEN_ROOT=/disposable/garden_room \
  EQ_URL=https://www.amazon.com/dp/ASIN python3 desktop/tests/live_garden_equipment.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("Z0LIVE_TEST_BASE", "http://127.0.0.1:18792")
GARDEN = Path(os.environ["GARDEN_ROOT"])
URL = os.environ["EQ_URL"]
ASIN = URL.rstrip("/").split("/dp/")[1][:10]
pair = subprocess.run([str(ROOT / "bin/z0live"), "pair", "garden-eq-test-" + uuid.uuid4().hex[:6]], capture_output=True, text=True, check=True).stdout
token = next(line.split("Device token: ", 1)[1].strip() for line in pair.splitlines() if "Device token: " in line)


def req(path, body=None):
    r = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                               headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


out = {"url": URL}
try:
    session = req("/v1/realtime/session", {"client": "web", "context": {"app": "garden-equipment-qualification", "test": True}, "tools": []})
    sid = session["session_id"]
    question = f"Add this plant stand to my garden equipment list and tell me whether it has an Amazon 3D model or an approximate one: {URL}"
    t0 = time.time()
    ack = req("/v1/tools/ask_z0", {"session_id": sid, "call_id": "eq-" + uuid.uuid4().hex[:8], "arguments": {"question": question}})
    did = ack.get("id")
    out["ack"] = {k: ack.get(k) for k in ("ok", "id", "status", "speech")}
    db = Path(os.environ["Z0LIVE_DATA"]) / "broker.db"
    row = None
    while did and time.time() - t0 < 900:
        time.sleep(3)
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status, route, run_id, marks, final FROM live_delegations WHERE id=?", (did,)).fetchone()
        if row and row[0] not in ("working", "running", "queued"):
            break
    out["answer_seconds"] = round(time.time() - t0, 1)
    out["delegation"] = row and {"status": row[0], "route": row[1], "run_id": row[2], "marks": json.loads(row[3] or "{}"), "final": row[4]}
    items = json.loads((GARDEN / "data/equipment/list.json").read_text())["items"]  # full records (the CLI list is a summary)
    hit = next((i for i in items if i.get("asin") == ASIN), None)
    out["garden_item"] = hit and {k: hit.get(k) for k in ("id", "title", "price", "dims", "source", "status")} | {"model": (hit.get("model") or {}).get("label")}
    assert row and row[0].startswith("completed") and row[4], "no completed delegated answer within 15 minutes"
    assert hit and hit.get("source") == "z0live", "equipment list was not updated by the delegated CLI"
    out["result"] = "PASS"
finally:
    for d in json.loads(subprocess.run([str(ROOT / "bin/z0live"), "devices"], capture_output=True, text=True).stdout or "[]"):
        if str(d.get("name", "")).startswith("garden-eq-test-") and not d.get("revoked"):
            subprocess.run([str(ROOT / "bin/z0live"), "revoke", d["id"]], capture_output=True)
    print(json.dumps(out, indent=1))
    Path(os.environ.get("Z0LIVE_TEST_OUTPUT", "/dev/null")).write_text(json.dumps(out, indent=1)) if os.environ.get("Z0LIVE_TEST_OUTPUT") else None
sys.exit(0 if out.get("result") == "PASS" else 1)
