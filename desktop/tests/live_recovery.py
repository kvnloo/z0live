"""Opt-in live API, local-action receipt and broker restart qualification (no phone)."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("Z0LIVE_TEST_BASE", "https://0.taila30dc.ts.net:8791")
DATA = ROOT.parents[1] / "data/z0live"
result = subprocess.check_output(
    [str(ROOT / "bin/z0live"), "pair", "headless-recovery-" + str(time.time_ns())],
    text=True,
)
TOKEN = result.split("Device token: ", 1)[1].splitlines()[0]


def request(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def jobs():
    with sqlite3.connect(DATA / "broker.db") as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute(
                "SELECT id,kind,status,run_id FROM jobs WHERE session_id=? ORDER BY id",
                (SID,),
            )
        ]


def wait(predicate, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("timed out waiting for live receipt")


session = request(
    "/v1/realtime/session",
    {
        "client": "assistant",
        "context": {"app": "headless-recovery"},
        "tools": [
            {
                "type": "function",
                "name": "local_echo",
                "description": "Headless client-local echo only",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ],
    },
)
SID = session["session_id"]
print(
    "PASS recovery session mint",
    json.dumps({"session_id": SID, "secret_length": len(session["value"])}),
)
context = request("/v1/tools/get_context", {"session_id": SID, "arguments": {}})
assert context["context"]["app"] == "headless-recovery"
print("PASS real get_context", json.dumps(context))
queued = json.loads(
    subprocess.check_output(
        [
            str(ROOT / "bin/z0live"),
            "action",
            "--session",
            SID,
            "--action",
            "local_echo",
            "--params",
            json.dumps({"text": "headless-local-receipt"}),
            "--say",
            "Headless action queued",
        ],
        text=True,
    )
)
inbox = request("/v1/inbox?after=" + str(queued["id"] - 1))
action = next(m for m in inbox["messages"] if m["id"] == queued["id"])
assert action["action"] == "local_echo"
# Execute a real harmless local handler, not an Android action or simulated device success.
local_result = {"echo": action["params"]["text"]}
request(
    "/v1/events",
    [
        {
            "session_id": SID,
            "ts": time.time(),
            "type": "tool_result",
            "text": "headless local echo completed",
            "data": {
                "event_id": "headless-local-result",
                "name": "local_echo",
                "result": local_result,
                "success": True,
                "executor": "headless-client-not-phone",
            },
        }
    ],
)
print(
    "PASS queued action delivered and headless-local execution receipt",
    json.dumps(
        {"id": action["id"], "action": action["action"], "result": local_result}
    ),
)
marker = "DIRECT_RECOVERY_" + str(time.time_ns())
job = request(
    "/v1/hermes/send",
    {
        "session_id": SID,
        "text": "[test] Reply exactly " + marker + ". No tools or edits.",
    },
)
print("PASS real /v1/hermes/send accepted", json.dumps(job))
submitted = wait(
    lambda: next((r for r in jobs() if r["id"] == job["id"] and r["run_id"]), None)
)
run_id = submitted["run_id"]
subprocess.run(["systemctl", "--user", "restart", "z0live.service"], check=True)
wait(
    lambda: (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "z0live.service"]
        ).returncode
        == 0
    )
)
finished = wait(
    lambda: next(
        (r for r in jobs() if r["id"] == job["id"] and r["status"] == "completed"), None
    )
)
assert finished["run_id"] == run_id
reply = wait(
    lambda: next(
        (
            m
            for m in request("/v1/inbox?after=" + str(action["id"]))["messages"]
            if marker in m["say"]
        ),
        None,
    )
)
print(
    "PASS in-flight broker restart preserved same native run + reply",
    json.dumps(
        {
            "job_id": job["id"],
            "run_id": run_id,
            "reply_id": reply["id"],
            "say": reply["say"],
        }
    ),
)
feedback = request(
    "/v1/events",
    [
        {
            "session_id": SID,
            "ts": time.time(),
            "type": "feedback",
            "text": "[test] Feedback transport check. Reply exactly FEEDBACK_OK; no edits or tools.",
            "data": {"event_id": "recovery-feedback"},
        }
    ],
)
assert feedback["accepted"] == 1
summary = request(
    "/v1/tools/summarize",
    {
        "session_id": SID,
        "arguments": {
            "text": "[test] Headless recovery test completed a local echo action, direct Hermes reply and feedback dispatch. No phone action is claimed. No edits needed."
        },
    },
)
end = request("/v1/sessions/" + SID + "/end", {})
wait(lambda: jobs() and all(r["status"] == "completed" for r in jobs()))
print(
    "PASS immediate feedback + GPT-supplied summary helper + end",
    json.dumps({"feedback": feedback, "summary": summary, "end": end, "jobs": jobs()}),
)
# Restart the API host only after all admitted work has settled; no reseeding.
with sqlite3.connect(DATA / "broker.db") as db:
    seeds_before = db.execute("SELECT count(*) FROM jobs WHERE kind='seed'").fetchone()[
        0
    ]
subprocess.run(["systemctl", "--user", "restart", "z0live-hermes.service"], check=True)
subprocess.run(["systemctl", "--user", "restart", "z0live.service"], check=True)
time.sleep(5)
assert request("/v1/health")["ok"]
assert any(s["id"] == SID and s["ended"] for s in request("/v1/sessions")["sessions"])
with sqlite3.connect(DATA / "broker.db") as db:
    assert (
        db.execute("SELECT count(*) FROM jobs WHERE kind='seed'").fetchone()[0]
        == seeds_before
    )
print(
    "PASS both services restart without reseeding; auth/history persisted",
    json.dumps({"seed_count": seeds_before, "health": request("/v1/health")}),
)
