"""Opt-in gesture control. No BLE, audio, credentials or raw motion in receipts."""

import json
import math
import os
import subprocess
import time
import uuid

COMPONENT = "app.lawnchair.play.debug/app.lawnchair.zer0.voice.z0live.Z0LiveActivity"
# A session whose telemetry moved within this window counts as live (the inbox staleness rule).
ADB_LIVE_SESSION_SECONDS = 120
# At most one adb relaunch per this many seconds, whatever the ring sends.
ADB_DEBOUNCE_SECONDS = 3


def adb_launch(gesture, runner=subprocess.run):
    """Only invoked by explicit broker opt-in. Resolve the sole tailnet transport each time."""
    started = time.monotonic()
    try:
        result = runner(
            ["adb", "devices"], capture_output=True, text=True, timeout=2, check=False
        )
        # Never select USB/emulator/unauthorized, or guess among multiple tailnet phones.
        serials = [
            line.split()[0]
            for line in result.stdout.splitlines()[1:]
            if len(line.split()) >= 2
            and line.split()[1] == "device"
            and line.split()[0].startswith("100.")
            and ":" in line.split()[0]
        ]
        if result.returncode or len(serials) != 1:
            return {"ok": False, "status": "adb_unavailable"}
        argv = [
            "adb",
            "-s",
            serials[0],
            "shell",
            "am",
            "start",
            "-n",
            COMPONENT,
            "--ez",
            "autostart",
            "true",
        ]
        if gesture == "triple_tap":
            argv += ["--es", "gesture", "triple_tap"]
        result = runner(argv, capture_output=True, text=True, timeout=3, check=False)
        ok = result.returncode == 0 and not any(
            s in result.stdout + result.stderr
            for s in ("Error", "Exception", "Permission Denial")
        )
        return {
            "ok": ok,
            "status": "activity_dispatched" if ok else "adb_start_failed",
            "dispatch_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "status": "adb_unavailable"}


class Gestures:
    def __init__(self, store, *, clock=time.time, adb=adb_launch):
        self.store, self.clock, self.adb = store, clock, adb
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS gesture_presence(device TEXT PRIMARY KEY, kind TEXT NOT NULL,
                seen REAL NOT NULL, activity REAL NOT NULL, available INTEGER NOT NULL, ready INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS gesture_listener(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                device TEXT NOT NULL, until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS gesture_pin(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                device TEXT NOT NULL, until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS gesture_events(source TEXT NOT NULL, nonce TEXT NOT NULL,
                created REAL NOT NULL, result TEXT NOT NULL, PRIMARY KEY(source,nonce));
            CREATE TABLE IF NOT EXISTS gesture_commands(id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, gesture TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued');
            """)

    def identity(self, device, body):
        if not isinstance(body, dict) or body.get("source_device") != device:
            raise ValueError("source_device must match authenticated device")

    def listener(self, device, body):
        self.identity(device, body)
        enabled, ttl = body.get("enabled"), body.get("ttl_seconds")
        if type(enabled) is not bool or type(ttl) is not int or not 1 <= ttl <= 300:
            raise ValueError("enabled boolean and ttl_seconds 1..300 required")
        now = self.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM gesture_listener WHERE singleton=1"
            ).fetchone()
            if enabled:
                if row and row["until"] > now and row["device"] != device:
                    raise ValueError(
                        "another ring listener owns the lease; turn it OFF first"
                    )
                db.execute(
                    "INSERT OR REPLACE INTO gesture_listener VALUES(1,?,?)",
                    (device, now + ttl),
                )
            else:
                db.execute("DELETE FROM gesture_listener WHERE device=?", (device,))
        return {"ok": True, "expires": now + ttl if enabled else now}

    def activity(self, device, body):
        now = self.clock()
        kind, at = body.get("kind"), body.get("activity_ts")
        if (
            kind not in {"phone", "desktop"}
            or type(at) not in {int, float}
            or not math.isfinite(at)
            or not 0 <= at <= now + 1
            or type(body.get("available")) is not bool
            or type(body.get("ready")) is not bool
        ):
            raise ValueError("kind, recent activity_ts, available and ready required")
        with self.store.connect() as db:
            # Refuse one credential claiming two target kinds.
            row = db.execute(
                "SELECT kind FROM gesture_presence WHERE device=?", (device,)
            ).fetchone()
            if row and row[0] != kind:
                raise ValueError("device target kind already registered")
            db.execute(
                "INSERT OR REPLACE INTO gesture_presence VALUES(?,?,?,?,?,?)",
                (device, kind, now, min(at, now), body["available"], body["ready"]),
            )
        return {"ok": True}

    def pin(self, device, body):
        ttl = body.get("ttl_seconds", 300)
        if type(ttl) is not int or not 0 <= ttl <= 300:
            raise ValueError("pin ttl_seconds must be 0..300")
        with self.store.connect() as db:
            if ttl:
                db.execute(
                    "INSERT OR REPLACE INTO gesture_pin VALUES(1,?,?)",
                    (device, self.clock() + ttl),
                )
            else:
                db.execute("DELETE FROM gesture_pin WHERE device=?", (device,))
        return {"ok": True}

    @staticmethod
    def adb_refusal(db, gesture, now):
        """Why the adb relaunch must not run, or None.

        Owner mapping: double tap with no call starts one, double tap in a call toggles mute,
        triple tap ends the call. The relaunch with `autostart` toggles mute when a call is
        already up, so it is used deterministically: a double only when no call is live (start),
        a triple only when one is (its `gesture=triple_tap` extra ends the call). Mute inside a
        call needs the live session's presence heartbeat and a queued command, never a relaunch.
        """
        live = db.execute(
            "SELECT 1 FROM sessions WHERE ended=0 AND client='lawnchair' AND updated>? LIMIT 1",
            (now - ADB_LIVE_SESSION_SECONDS,),
        ).fetchone()
        if gesture == "double_tap" and live:
            return "session_live_no_presence"
        if gesture == "triple_tap" and not live:
            return "no_call_to_end"
        recent = db.execute(
            "SELECT 1 FROM gesture_events WHERE created>? AND json_extract(result,'$.adb')=1 LIMIT 1",
            (now - ADB_DEBOUNCE_SECONDS,),
        ).fetchone()
        return "adb_debounced" if recent else None

    def submit(self, device, body):
        self.identity(device, body)
        gesture, ts, nonce = (body.get(k) for k in ("gesture", "ts", "nonce"))
        if gesture not in {"double_tap", "triple_tap"}:
            raise ValueError("unsupported gesture")
        if not isinstance(nonce, str) or str(uuid.UUID(nonce)) != nonce:
            raise ValueError("canonical UUID nonce required")
        now = self.clock()
        if type(ts) not in {int, float} or not math.isfinite(ts) or abs(now - ts) > 1.5:
            raise ValueError("gesture must be fresh within 1.5 seconds")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM ring_recordings WHERE source=? AND expires>? AND desired!='done'", (device, now)).fetchone():
                return {"ok": True, "status": "recording_suppressed"}
            old = db.execute(
                "SELECT result FROM gesture_events WHERE source=? AND nonce=?",
                (device, nonce),
            ).fetchone()
            if old:
                return {**json.loads(old[0]), "duplicate": True}
            lease = db.execute(
                "SELECT * FROM gesture_listener WHERE singleton=1"
            ).fetchone()
            if not lease or lease["device"] != device or lease["until"] <= now:
                raise ValueError("live listener lease required")
            rows = db.execute(
                "SELECT * FROM gesture_presence WHERE seen>? AND available=1",
                (now - 15,),
            ).fetchall()
            # Revoked credentials cannot remain routing targets.
            valid = {
                d["id"]
                for d in json.loads((self.store.directory / "devices.json").read_text())
                if not d.get("revoked")
            }
            rows = [r for r in rows if r["device"] in valid]
            pin = db.execute(
                "SELECT * FROM gesture_pin WHERE singleton=1 AND until>?", (now,)
            ).fetchone()
            target = next(
                (r for r in rows if pin and r["device"] == pin["device"]), None
            )
            if target is None and rows:
                target = max(
                    rows,
                    key=lambda r: (r["activity"], r["kind"] == "phone", r["device"]),
                )
            result = {"ok": False, "status": "no_active_target"}
            if target is None and os.environ.get("Z0LIVE_GESTURE_ADB") == "1":
                # No surface heartbeated in the last 15 s (Lawnchair Standby dozes on Android),
                # so the gesture would be dropped. With the owner's adb opt-in, wake the phone's
                # voice activity directly instead; the nonce is reserved first, as below.
                result = {"ok": True, "status": "adb_reserved", "fallback": "no_presence"}
            elif target:
                result = {"ok": True, "status": "queued", "target": target["kind"]}
                if target["kind"] == "phone" and not target["ready"]:
                    # Reserve nonce BEFORE external effect; crash recovery must never repeat a toggle.
                    result["status"] = (
                        "adb_reserved"
                        if os.environ.get("Z0LIVE_GESTURE_ADB") == "1"
                        else "phone_not_ready"
                    )
                    result["ok"] = result["status"] == "adb_reserved"
                else:
                    cursor = db.execute(
                        "INSERT INTO gesture_commands(device,gesture,created,expires) VALUES(?,?,?,?)",
                        (target["device"], gesture, now, now + 3),
                    )
                    result["command_id"] = cursor.lastrowid
            if result["status"] == "adb_reserved":
                blocked = self.adb_refusal(db, gesture, now)
                if blocked:
                    result = {"ok": False, "status": blocked, "fallback": result.get("fallback")}
                else:
                    result["adb"] = True
            db.execute(
                "INSERT INTO gesture_events VALUES(?,?,?,?)",
                (device, nonce, now, json.dumps(result)),
            )
            # Replay horizon exceeds freshness; cap historical storage without forgetting live identities.
            db.execute("DELETE FROM gesture_events WHERE created<?", (now - 86400,))
            db.execute("DELETE FROM gesture_commands WHERE expires<?", (now - 86400,))
        if result["status"] == "adb_reserved":
            result = {**result, **self.adb(gesture), "target": "phone"}
            with self.store.connect() as db:
                db.execute(
                    "UPDATE gesture_events SET result=? WHERE source=? AND nonce=?",
                    (json.dumps(result), device, nonce),
                )
        return result

    def commands(self, device):
        now = self.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,gesture,expires FROM gesture_commands WHERE device=? AND status='queued' AND expires>? ORDER BY id LIMIT 8",
                (device, now),
            ).fetchall()
            # At-most-once claim, not proof of capture; client death loses a command safely.
            for row in rows:
                db.execute(
                    "UPDATE gesture_commands SET status='claimed' WHERE id=?",
                    (row["id"],),
                )
        return {"commands": [dict(r) for r in rows]}

    def ack(self, device, body):
        if type(body.get("id")) is not int or body.get("status") not in {
            "started",
            "muted",
            "ended",
            "denied",
            "failed",
        }:
            raise ValueError("command id and closed status required")
        with self.store.connect() as db:
            row = db.execute(
                "UPDATE gesture_commands SET status=? WHERE id=? AND device=? AND status='claimed'",
                (body["status"], body["id"], device),
            )
        return {"ok": row.rowcount == 1}
