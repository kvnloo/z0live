"""Pinned VoicePlan consumer, native Hermes transport and per-turn observations."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
import uuid

from .core import NoRedirect, dumps, request_json

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "timeout"}
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def load_plan():
    path = Path(
        os.environ.get(
            "Z0LIVE_VOICE_PLAN",
            str(
                Path(os.environ.get("Z0INT_HOME", str(Path.home() / ".z0int")))
                / "config/z0live-voice-plan.json"
            ),
        )
    )
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("schema") != "z0int.voice_plan.v1" or set(value.get("roles", {})) != {
        "smol",
        "default",
        "plan",
        "slow",
        "task",
        "judge",
    }:
        raise RuntimeError("invalid z0intelligence voice profile")
    for binding in value["roles"].values():
        if set(binding) != {"provider", "model"} or any(
            not isinstance(v, str) or not IDENT.fullmatch(v) for v in binding.values()
        ):
            raise RuntimeError("invalid voice role binding")
    if (
        value.get("meta_role") not in {"default", "plan", "slow"}
        or value.get("fallbacks") != []
    ):
        raise RuntimeError("invalid voice plan policy")
    # This host adapter only implements the ChatGPT WebRTC actor; reject, never substitute.
    if (
        value.get("model"),
        value.get("voice"),
        value.get("adapter"),
        value.get("harness"),
    ) != ("gpt-realtime", "marin", "desktop-webrtc", "hermes"):
        raise RuntimeError("unsupported ConversationActor plan")
    unsigned = {k: v for k, v in value.items() if k != "digest"}
    digest = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if value.get("digest") != digest:
        raise RuntimeError("voice plan digest mismatch; export with z0intelligence")
    return value


class Native:
    def __init__(self, store):
        self.store = store
        self.base = os.environ.get("Z0LIVE_HERMES_API", "http://127.0.0.1:8644")
        self.liaison = os.environ.get("Z0LIVE_LIAISON_API", "http://127.0.0.1:8645")

    def headers(self, idem=None):
        key = (self.store.directory / ".hermes-api-key").read_text().strip()
        return {
            "Authorization": "Bearer " + key,
            "X-Hermes-Session-Key": "z0live",
            **({"Idempotency-Key": idem} if idem else {}),
        }

    def api(self, path, body=None, idem=None):
        return request_json(self.base + path, body, self.headers(idem), timeout=5)

    def stream(self, url, body=None, last=None):
        headers = {"Content-Type": "application/json", **self.headers()}
        if last is not None:
            headers["Last-Event-ID"] = str(last)
        req = urllib.request.Request(
            url,
            data=dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            for line in response:
                if line.startswith(b"data: "):
                    yield json.loads(line[6:])

    def small(self, turn, binding, prompt, on_delta=None):
        final = None
        for event in self.stream(
            self.liaison + "/complete",
            {"turn_id": turn, "binding": binding, "prompt": prompt},
        ):
            if event["type"] == "delta" and on_delta:
                on_delta(event["text"])
            if event["type"] == "error":
                raise RuntimeError("liaison inference " + event["error"])
            if event["type"] == "done":
                final = event
        if not final or final.get("cancelled"):
            raise RuntimeError("liaison interrupted")
        return final

    def cancel_liaison(self, turn):
        return request_json(
            self.liaison + "/cancel", {"turn_id": turn}, self.headers(), timeout=2
        )


class Journal:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.directory = store.directory / "traces"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS voice_plans(sid TEXT PRIMARY KEY, plan TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_turns(id TEXT PRIMARY KEY,sid TEXT NOT NULL,call_key TEXT UNIQUE,
                question TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                job_id INTEGER,brief TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS voice_runs(turn_id TEXT NOT NULL,node TEXT NOT NULL,role TEXT NOT NULL,
                model TEXT NOT NULL,run_id TEXT,status TEXT NOT NULL,request TEXT,created REAL NOT NULL,
                updated REAL NOT NULL,usage TEXT,output TEXT,PRIMARY KEY(turn_id,node));
            CREATE TABLE IF NOT EXISTS voice_trace(id INTEGER PRIMARY KEY AUTOINCREMENT,turn_id TEXT NOT NULL,
                sid TEXT NOT NULL,ts REAL NOT NULL,layer TEXT NOT NULL,role TEXT,model TEXT,event TEXT NOT NULL,
                ms REAL,usage TEXT,cost_usd REAL,details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_floor(sid TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_speech(id INTEGER PRIMARY KEY AUTOINCREMENT,sid TEXT NOT NULL,
                turn_id TEXT NOT NULL,say TEXT NOT NULL,params TEXT NOT NULL,source TEXT UNIQUE NOT NULL);
            """)

    def plan(self, sid):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT plan FROM voice_plans WHERE sid=?", (sid,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def trace(
        self, turn, layer, event, role=None, model=None, ms=None, usage=None, **details
    ):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT sid FROM voice_turns WHERE id=?", (turn,)
            ).fetchone()
            if not row:
                raise LookupError("unknown voice turn")
            db.execute(
                "INSERT INTO voice_trace(turn_id,sid,ts,layer,role,model,event,ms,usage,cost_usd,details) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    turn,
                    row[0],
                    time.time(),
                    layer,
                    role,
                    model,
                    event,
                    ms,
                    dumps(usage) if usage else None,
                    None,
                    dumps(details),
                ),
            )
        self.export(turn)

    def read(self, turn=None, sid=None, limit=2000):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM voice_trace WHERE (? IS NULL OR turn_id=?) AND (? IS NULL OR sid=?) ORDER BY id DESC LIMIT ?",
                (turn, turn, sid, sid, limit),
            ).fetchall()
        return [
            {
                **dict(r),
                "usage": json.loads(r["usage"]) if r["usage"] else None,
                "details": json.loads(r["details"]),
                "cost_status": "unknown: provider billing/pricing not supplied",
            }
            for r in reversed(rows)
        ]

    def export(self, turn):
        if not re.fullmatch(r"turn-[a-f0-9]{24}", turn):
            raise ValueError("invalid trace ID")
        with self.lock:
            path = self.directory / (turn + ".jsonl")
            temp = self.directory / (turn + "." + uuid.uuid4().hex + ".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                for row in self.read(turn):
                    f.write(dumps(row) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)

    def cancelled(self, turn):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT status FROM voice_turns WHERE id=?", (turn,)
            ).fetchone()
        return not row or row[0] in {"cancelling", "cancelled", "interrupted"}

    def queue_speech(self, db, turn, say, event, **params):
        row = db.execute(
            "SELECT sid,status FROM voice_turns WHERE id=?", (turn,)
        ).fetchone()
        if not row or row["status"] in {"cancelling", "cancelled", "interrupted"}:
            return
        session = db.execute(
            "SELECT device,ended,updated FROM sessions WHERE id=?", (row["sid"],)
        ).fetchone()
        latest = db.execute(
            "SELECT id FROM sessions WHERE device=? ORDER BY created DESC,rowid DESC LIMIT 1",
            (session["device"],),
        ).fetchone()
        if (
            session["ended"]
            or latest[0] != row["sid"]
            or time.time() - session["updated"] > 120
        ):
            return
        payload = dumps({"turn_id": turn, "event": event, **params})
        source = "voice:" + turn + ":" + event
        floor = db.execute(
            "SELECT state FROM voice_floor WHERE sid=?", (row["sid"],)
        ).fetchone()
        if floor and floor[0] != "quiet":
            db.execute(
                "INSERT OR IGNORE INTO voice_speech(sid,turn_id,say,params,source) VALUES(?,?,?,?,?)",
                (row["sid"], turn, say[:700], payload, source),
            )
        else:
            db.execute(
                "INSERT OR IGNORE INTO inbox(device,session_id,say,action,params,created,source) VALUES(?,?,?,NULL,?,?,?)",
                (
                    session["device"],
                    row["sid"],
                    say[:700],
                    payload,
                    time.time(),
                    source,
                ),
            )

    def speak(self, turn, say, event, **params):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.queue_speech(db, turn, say, event, **params)

    def flush(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Expired/superseded/cancelled speech is a discarded backlog, not deferred audio.
            db.execute("""DELETE FROM voice_speech WHERE turn_id IN
                (SELECT id FROM voice_turns WHERE status IN ('cancelling','cancelled','interrupted'))
                OR sid IN (SELECT s.id FROM sessions s WHERE s.ended=1
                    OR s.updated < strftime('%s','now')-120
                    OR s.id != (SELECT id FROM sessions WHERE device=s.device ORDER BY created DESC,rowid DESC LIMIT 1))""")
            rows = db.execute("""SELECT v.*,s.device FROM voice_speech v JOIN sessions s ON s.id=v.sid
                LEFT JOIN voice_floor f ON f.sid=v.sid WHERE (f.state IS NULL OR f.state='quiet') AND s.ended=0 AND s.updated > strftime('%s','now')-120 AND s.id=(SELECT id FROM sessions WHERE device=s.device ORDER BY created DESC,rowid DESC LIMIT 1) ORDER BY v.id""").fetchall()
            for row in rows:
                db.execute(
                    "INSERT OR IGNORE INTO inbox(device,session_id,say,action,params,created,source) VALUES(?,?,?,NULL,?,?,?)",
                    (
                        row["device"],
                        row["sid"],
                        row["say"],
                        row["params"],
                        time.time(),
                        row["source"],
                    ),
                )
                db.execute("DELETE FROM voice_speech WHERE id=?", (row["id"],))

    def fanout(self, turn, briefs, native=None):
        if not 1 <= len(briefs) <= 4 or any(
            not isinstance(b, str) or not b.strip() or len(b) > 12000 for b in briefs
        ):
            raise ValueError("fanout needs 1..4 bounded briefs")
        native = native or Native(self.store)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT sid,status FROM voice_turns WHERE id=?", (turn,)
            ).fetchone()
        if not row or self.cancelled(turn):
            raise ValueError("turn is not active")
        plan = self.plan(row["sid"])
        binding = plan["roles"]["task"]
        self.speak(
            turn, f"{len(briefs)} agents are checking the context.", "workers_started"
        )

        def child(index, brief):
            node = "worker-" + str(index)
            sid = "z0live-" + turn + "-" + node
            body = {
                "session_id": sid,
                "provider": binding["provider"],
                "model": binding["model"],
                "input": "You are a bounded read-only z0live context worker, not the meta agent. Use tools for facts; do not edit, publish, deploy, delegate, message people or change credentials. Preserve citations and uncertainty. Run terminal commands with nice -n 10 and explicit workdir. Return a concise evidence summary.\n"
                + brief,
            }
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if self.cancelled(turn):
                    return {"node": node, "status": "cancelled"}
                row = db.execute(
                    "SELECT * FROM voice_runs WHERE turn_id=? AND node=?", (turn, node)
                ).fetchone()
                if row and row["request"] != dumps(body):
                    raise ValueError("worker slot already used with different brief")
                if row and (row["status"] in TERMINAL or row["status"] == "unknown"):
                    return {
                        "node": node,
                        "status": row["status"],
                        "output": row["output"],
                        "run_id": row["run_id"],
                    }
                if not row:
                    now = time.time()
                    db.execute(
                        "INSERT INTO voice_runs(turn_id,node,role,model,status,request,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            turn,
                            node,
                            "task",
                            binding["model"],
                            "queued",
                            dumps(body),
                            now,
                            now,
                        ),
                    )
            start = time.monotonic()
            run_id = row["run_id"] if row else None
            submitting = False
            try:
                if not run_id:
                    try:
                        native.api("/api/sessions/" + sid)
                    except RuntimeError as exc:
                        if "HTTP 404" not in str(exc):
                            raise
                        native.api(
                            "/api/sessions",
                            {
                                "id": sid,
                                "title": sid,
                                "source": "api_server",
                                **binding,
                                "require_model_lock": True,
                            },
                        )
                    if self.cancelled(turn):
                        raise InterruptedError()
                    submitting = True
                    run_id = native.api("/v1/runs", body, idem=turn + "-" + node)[
                        "run_id"
                    ]
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE voice_runs SET run_id=?,status='running',updated=? WHERE turn_id=? AND node=?",
                            (run_id, time.time(), turn, node),
                        )
                    self.trace(
                        turn,
                        "L3",
                        "started",
                        "task",
                        binding["model"],
                        node=node,
                        run_id=run_id,
                    )
                deadline = time.monotonic() + float(
                    os.environ.get("Z0LIVE_WORKER_SECONDS", "120")
                )
                stopped = False
                while True:
                    if (
                        self.cancelled(turn) or time.monotonic() > deadline
                    ) and not stopped:
                        native.api("/v1/runs/" + run_id + "/stop", {})
                        stopped = True
                        self.trace(
                            turn,
                            "L3",
                            "cancel_requested",
                            "task",
                            binding["model"],
                            node=node,
                            run_id=run_id,
                        )
                    result = native.api("/v1/runs/" + run_id)
                    if result["status"] in TERMINAL:
                        break
                    if stopped and time.monotonic() > deadline + 20:
                        raise RuntimeError(
                            "native worker cancellation not yet confirmed"
                        )
                    time.sleep(0.2)
                status = result["status"]
                usage = result.get("usage")
                output = result.get("output", "")
                runtime = result.get("runtime")
                self.trace(
                    turn,
                    "L3",
                    status,
                    "task",
                    binding["model"],
                    ms=round((time.monotonic() - start) * 1000),
                    usage=usage,
                    node=node,
                    run_id=run_id,
                    runtime=runtime,
                )
                if status == "completed":
                    self.speak(
                        turn, "One agent has returned its findings.", node + "_finished"
                    )
            except InterruptedError:
                status, output, usage = "cancelled", "", None
            except Exception as exc:
                # A lost acceptance is not failure/cancellation proof. Preserve uncertainty
                # and the exact request/key; never create a fresh untracked replacement.
                status = (
                    "stop_pending" if run_id else "unknown" if submitting else "failed"
                )
                output, usage = type(exc).__name__, None
                if run_id:
                    try:
                        native.api("/v1/runs/" + run_id + "/stop", {})
                    except Exception:
                        pass
                self.trace(
                    turn,
                    "L3",
                    status,
                    "task",
                    binding["model"],
                    node=node,
                    run_id=run_id,
                    error=type(exc).__name__,
                )
            with self.store.connect() as db:
                db.execute(
                    "UPDATE voice_runs SET status=?,output=?,usage=?,updated=? WHERE turn_id=? AND node=?",
                    (status, output, dumps(usage), time.time(), turn, node),
                )
            return {"node": node, "status": status, "output": output, "run_id": run_id}

        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="voice-child"
        ) as pool:
            futures = [pool.submit(child, i, brief) for i, brief in enumerate(briefs)]
            return [f.result() for f in futures]
