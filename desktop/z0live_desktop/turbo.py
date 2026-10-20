"""Durable voice admission for z0int turbo. Placement and process stops belong to z0int."""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid

from .core import dumps, text
from .voice_runtime import TERMINAL
from .work_schema import TURBO_TOOLS as TOOLS

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
# z0int turbo owns these terminal states. `deadline` is Turbo-only: the shared voice
# terminal set stays unchanged so Hermes/L1 cancellation semantics are not widened.
TURBO_TERMINAL = TERMINAL | {"deadline"}
# States that must never be overwritten by process exit/reconciliation.
TURBO_SETTLED = TURBO_TERMINAL | {"cancelling"}
# Actual z0int runtime events that carry a verified progress summary; startup/waiting
# events are observability only and are not spoken.
TURBO_PROGRESS = {"progress", "plan_validated", "node_started", "node_completed",
                  "node_failed", "verifying"}
# The runtime enforces its own hard stop; never let a voice caller request longer.
MAX_DEADLINE_SECONDS = 3600


class Turbo:
    def __init__(self, store):
        self.store = store
        self.journal = store.voice.journal
        self.lock = threading.Lock()
        self.running = set()
        self.polling = set()
        self.next_poll = 0
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS turbo_runs(
                  task_id TEXT PRIMARY KEY, sid TEXT NOT NULL, call_key TEXT UNIQUE NOT NULL,
                  args TEXT NOT NULL, run_id TEXT UNIQUE, status TEXT NOT NULL,
                  stop_requested INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turbo_events(
                  task_id TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(task_id,digest));
            """)

    @staticmethod
    def executable():
        return os.environ.get("Z0LIVE_Z0INT", "z0int")

    @staticmethod
    def valid_id(value):
        if not isinstance(value, str) or not RUN_ID.fullmatch(value):
            raise ValueError("invalid turbo run_id")
        return value

    @staticmethod
    def valid_deadline(value):
        match = re.fullmatch(r"([1-9][0-9]{0,4})([smh])", value) if isinstance(value, str) else None
        if not match:
            raise ValueError("deadline must be a positive duration such as 10m")
        seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
        if seconds > MAX_DEADLINE_SECONDS:
            raise ValueError("deadline must be at most 1h")
        return value

    def execute(self, sid, name, args, call_id=None):
        spec = next(t["parameters"] for t in TOOLS if t["name"] == name)
        if set(args) - set(spec["properties"]) or any(k not in args for k in spec["required"]):
            raise ValueError("invalid turbo arguments")
        for key, value in args.items():
            text(value, 16000 if key == "task" else 4096)
            if "\0" in value:
                raise ValueError("NUL in turbo argument")
        if name == "turbo_run":
            return self.admit(sid, args, call_id)
        row = self.find(sid, args.get("run_id"))
        if not row:
            return {"ok": True, "status": "idle", "say": "There is no Turbo run in this session."}
        if name == "turbo_stop":
            self.request_stop(row)
        elif row["run_id"]:
            self.refresh(row)
        return self.result(self.find(sid, row["run_id"], row["task_id"]))

    def admit(self, sid, args, call_id):
        args = {"task": args["task"], "deadline": args.get("deadline", "10m"),
                **({"repo": args["repo"]} if "repo" in args else {})}
        self.valid_deadline(args["deadline"])
        if "repo" in args and (not os.path.isabs(args["repo"]) or args["repo"].startswith("-")):
            raise ValueError("repo must be an absolute path")
        payload = dumps(args)
        key = sid + ":turbo:" + (text(call_id, 200) if call_id is not None else hashlib.sha256(payload.encode()).hexdigest())
        turn = "turn-" + uuid.uuid4().hex[:24]
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            latest = db.execute("SELECT id FROM sessions WHERE device=? ORDER BY created DESC,rowid DESC LIMIT 1", (session["device"],)).fetchone()
            if session["ended"] or latest[0] != sid or now - session["updated"] > 120:
                raise ValueError("Turbo requires an active human-started session")
            old = db.execute("SELECT * FROM turbo_runs WHERE call_key=?", (key,)).fetchone()
            if old:
                if old["args"] != payload:
                    raise ValueError("call_id reused with different arguments")
                return self.result(old)
            terminal = sorted(TURBO_TERMINAL)
            marks = ",".join("?" for _ in terminal)
            if db.execute(
                "SELECT count(*) FROM turbo_runs WHERE status NOT IN (%s)" % marks,
                terminal,
            ).fetchone()[0] >= 8:
                raise RuntimeError("Turbo queue full")
            db.execute("INSERT INTO voice_turns(id,sid,question,status,created,updated) VALUES(?,?,?,'turbo_queued',?,?)", (turn, sid, args["task"], now, now))
            db.execute("INSERT INTO turbo_runs(task_id,sid,call_key,args,status,created,updated) VALUES(?,?,?,?,'queued',?,?)", (turn, sid, key, payload, now, now))
        self.tick()
        return {"ok": True, "task_id": turn, "turn_id": turn, "run_id": None, "status": "queued", "say": "Turbo is queued. I'll bring back its result."}

    def find(self, sid, run_id=None, task_id=None):
        if run_id is not None:
            self.valid_id(run_id)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM turbo_runs WHERE sid=? AND (? IS NULL OR run_id=?) AND (? IS NULL OR task_id=?) ORDER BY created DESC,rowid DESC LIMIT 1", (sid, run_id, run_id, task_id, task_id)).fetchone()
        if run_id and not row:
            raise LookupError("Turbo run not found in this session")
        return dict(row) if row else None

    def result(self, row):
        failed = {"failed", "unknown", "cancelled", "interrupted", "timeout", "deadline"}
        value = {"ok": row["status"] not in failed, "task_id": row["task_id"],
                 "turn_id": row["task_id"], "run_id": row["run_id"], "status": row["status"],
                 "stop_requested": bool(row["stop_requested"]), "summary": row["summary"],
                 "say": "Turbo is " + row["status"] + "."}
        # A terminal status receipt carries quality/results. Preserve them verbatim.
        with self.store.connect() as db:
            event = db.execute("SELECT body FROM turbo_events WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                               (row["task_id"],)).fetchone()
        if event:
            receipt = json.loads(event["body"])
            for key in ("quality", "results"):
                if key in receipt:
                    value[key] = receipt[key]
        return value

    def command(self, *argv):
        proc = subprocess.run([self.executable(), "turbo", *argv], shell=False,
                              capture_output=True, text=True, timeout=10)
        if proc.returncode:
            raise RuntimeError("z0int turbo command failed")
        value = json.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError("invalid z0int response")
        return value

    def request_stop(self, row):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM turbo_runs WHERE task_id=?", (row["task_id"],)).fetchone()
            if not current or current["status"] in TURBO_TERMINAL:
                # Already finished (or gone). Never rewrite a settled outcome as cancelling.
                return
            if current["status"] == "queued":
                # Nothing was launched: this is a real cancellation, not a pending stop.
                db.execute("UPDATE turbo_runs SET stop_requested=1,status='cancelled' WHERE task_id=?", (row["task_id"],))
                db.execute("UPDATE voice_turns SET status='cancelled',updated=? WHERE id=?", (time.time(), row["task_id"]))
            else:
                db.execute("UPDATE turbo_runs SET stop_requested=1,status='cancelling' WHERE task_id=?", (row["task_id"],))
                db.execute("UPDATE voice_turns SET status='cancelling',updated=? WHERE id=?", (time.time(), row["task_id"]))
            db.execute("DELETE FROM voice_speech WHERE turn_id=?", (row["task_id"],))
            current = db.execute("SELECT * FROM turbo_runs WHERE task_id=?", (row["task_id"],)).fetchone()
            run_id, status = current["run_id"], current["status"]
        if run_id and status not in TURBO_TERMINAL:
            try:
                value = self.command("stop", self.valid_id(run_id), "--json")
                if value.get("run_id") not in (None, run_id):
                    raise ValueError("z0int stop run mismatch")
                # A stop request and `terminal_confirmed` are not the terminal outcome.
                # Always read the runtime's `state` before claiming completion/cancellation.
                self.refresh(dict(current))
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass  # Durable cancellation remains pending for reconciliation.

    def cancel_session(self, sid):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM turbo_runs WHERE sid=?", (sid,)).fetchall()
        for row in rows:
            self.request_stop(dict(row))

    def refresh(self, row):
        if row["status"] in TURBO_TERMINAL:
            return  # A durable terminal receipt is authoritative; no CLI replay needed.
        value = self.command("status", self.valid_id(row["run_id"]), "--json")
        if value.get("run_id") != row["run_id"]:
            raise ValueError("z0int status run mismatch")
        state = text(value.get("state"), 100)
        with self.store.connect() as db:
            current = db.execute("SELECT * FROM turbo_runs WHERE task_id=?", (row["task_id"],)).fetchone()
        stop_requested = bool(current["stop_requested"]) if current else bool(row["stop_requested"])
        if state in TURBO_TERMINAL:
            quality = value.get("quality") if isinstance(value.get("quality"), dict) else {}
            summary = value.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                summary = quality.get("answer") or quality.get("reason")
            if not isinstance(summary, str) or not summary.strip():
                summary = "Turbo run " + state
            receipt = {"run_id": row["run_id"], "event": state, "summary": summary[:16000]}
            for key in ("quality", "results"):
                if key in value:
                    receipt[key] = value[key]
            self.event(row["task_id"], receipt)
        elif not stop_requested:
            settled = sorted(TURBO_SETTLED)
            marks = ",".join("?" for _ in settled)
            with self.store.connect() as db:
                changed = db.execute(
                    "UPDATE turbo_runs SET status=?,updated=? WHERE task_id=? AND status NOT IN (%s)" % marks,
                    (state, time.time(), row["task_id"], *settled),
                ).rowcount
                if changed:
                    db.execute(
                        "UPDATE voice_turns SET status='turbo_running',updated=? WHERE id=? AND status NOT IN ('cancelling','cancelled')",
                        (time.time(), row["task_id"]),
                    )

    def event(self, turn, value):
        if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
            raise ValueError("invalid turbo event")
        run_id = self.valid_id(value.get("run_id"))
        kind = text(value.get("event"), 100)
        summary = value["summary"][:16000]
        terminal = kind in TURBO_TERMINAL
        digest = hashlib.sha256(dumps([kind, summary]).encode()).hexdigest()
        self.journal.flush()  # Drop expired queued speech before activity can revive it.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turbo_runs WHERE task_id=?", (turn,)).fetchone()
            if not row:
                raise LookupError("unknown turbo run")
            if row["run_id"] and row["run_id"] != run_id:
                raise ValueError("turbo stream run mismatch")
            if row["status"] in TURBO_TERMINAL:
                return False
            db.execute("UPDATE turbo_runs SET run_id=? WHERE task_id=?", (run_id, turn))
            inserted = db.execute("INSERT OR IGNORE INTO turbo_events VALUES(?,?,?)", (turn, digest, dumps(value))).rowcount
            status = kind if terminal else "cancelling" if row["stop_requested"] else "running"
            db.execute("UPDATE turbo_runs SET status=?,summary=?,updated=? WHERE task_id=?", (status, summary, time.time(), turn))
            # Use the existing L1 outbox, floor, latest-session and cancellation gates.
            speak = inserted and summary and not row["stop_requested"] and (terminal or kind in TURBO_PROGRESS)
            if speak:
                self.journal.queue_speech(db, turn, summary, "turbo:" + ("final" if terminal else digest), run_id=run_id, task_id=turn, result=value)
            db.execute("UPDATE voice_turns SET status=?,updated=? WHERE id=?", (status if terminal else "cancelling" if row["stop_requested"] else "turbo_running", time.time(), turn))
        # A stream terminal event also clears a pending stop: do not ask to stop a finished run.
        return bool(row["stop_requested"]) and not terminal

    def launch(self, row):
        turn = row["task_id"]
        proc = None
        try:
            with self.store.connect() as db:
                current = db.execute("SELECT * FROM turbo_runs WHERE task_id=?", (turn,)).fetchone()
                if current["status"] in TURBO_TERMINAL:
                    return
                if current["stop_requested"]:
                    db.execute("UPDATE turbo_runs SET status='cancelled' WHERE task_id=?", (turn,))
                    db.execute("UPDATE voice_turns SET status='cancelled' WHERE id=?", (turn,))
                    return
            args = json.loads(row["args"])
            argv = [self.executable(), "turbo", "run", "--task", args["task"]]
            if "repo" in args:
                argv += ["--repo", args["repo"]]
            argv += ["--deadline", args["deadline"], "--jsonl"]
            proc = subprocess.Popen(argv, shell=False, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            assert proc.stdout is not None
            with proc.stdout:
                while True:
                    line = proc.stdout.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        raise ValueError("oversized turbo event")
                    if self.event(turn, json.loads(line)):
                        current = self.find(row["sid"], task_id=turn)
                        self.request_stop(current)
            proc.wait()
            settled = sorted(TURBO_SETTLED)
            with self.store.connect() as db:
                # EOF/exit code alone does not prove task success or cancellation.
                db.execute(
                    "UPDATE turbo_runs SET status='unknown' WHERE task_id=? AND status NOT IN (%s)"
                    % ",".join("?" for _ in settled),
                    (turn, *settled),
                )
        except Exception as exc:
            settled = sorted(TURBO_SETTLED)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE turbo_runs SET status=?,summary=? WHERE task_id=? AND status NOT IN (%s)"
                    % ",".join("?" for _ in settled),
                    ("failed" if isinstance(exc, OSError) and proc is None else "unknown", type(exc).__name__, turn, *settled),
                )
        finally:
            # Never kill a process tree. CLI stop writes the run's cancellation marker.
            if proc is not None and proc.poll() is None:
                current = self.find(row["sid"], task_id=turn)
                if current and current["run_id"]:
                    self.request_stop(current)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass  # Unresponsive own CLI: the durable stop marker stays for reconciliation.
            with self.lock:
                self.running.discard(turn)

    def tick(self):
        with self.lock:
            if time.monotonic() >= self.next_poll:
                self.next_poll = time.monotonic() + 3
                terminal = sorted(TURBO_TERMINAL)
                with self.store.connect() as db:
                    pending = db.execute(
                        "SELECT * FROM turbo_runs WHERE run_id IS NOT NULL AND status NOT IN (%s)"
                        % ",".join("?" for _ in terminal),
                        terminal,
                    ).fetchall()
                for row in pending:
                    if row["task_id"] not in self.polling:
                        self.polling.add(row["task_id"])
                        threading.Thread(target=self.reconcile, args=(dict(row),), daemon=True, name="turbo-status").start()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT * FROM turbo_runs WHERE status='queued'").fetchall()
                for row in rows:
                    # Persist before exec. A restart in this gap is unknown, never re-executed.
                    db.execute("UPDATE turbo_runs SET status='starting' WHERE task_id=?", (row["task_id"],))
            for row in rows:
                self.running.add(row["task_id"])
                threading.Thread(target=self.launch, args=(dict(row),), daemon=True, name="voice-turbo").start()

    def reconcile(self, row):
        try:
            if row["stop_requested"]:
                self.request_stop(row)
            else:
                self.refresh(row)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            with self.lock:
                self.polling.discard(row["task_id"])

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE turbo_runs SET status='unknown' WHERE status IN ('starting','running')")

        # Poll in background: recovery never blocks voice on multiple CLI timeouts.
        self.next_poll = 0
        self.tick()
