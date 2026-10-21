"""Owner-opted-in accessibility recovery. No UI input, installs or voice restarts."""

import ipaddress
import os
import shlex
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

PACKAGE = "app.lawnchair.play.debug"
SERVICE = PACKAGE + "/app.lawnchair.zer0.voice.z0live.Z0LiveAccessibilityService"
KEY = "enabled_accessibility_services"
_LOCK = threading.Lock()


def adb_restore(runner=subprocess.run):
    # Do not inherit an unrelated forwarded ADB server. Never log shell output/preferences.
    env = {k: v for k, v in os.environ.items() if k != "ADB_SERVER_SOCKET"}

    def run(args):
        result = runner(["adb", *args], capture_output=True, text=True, timeout=3, check=False, env=env)
        if result.returncode or any(s in result.stdout + result.stderr for s in ("Exception", "Permission Denial", "Error:")):
            raise OSError("adb command failed")
        return result.stdout.strip()

    try:
        serials = []
        for line in run(["devices"]).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or parts[1] != "device":
                continue
            host, sep, port = parts[0].rpartition(":")
            try:
                if sep and port.isdigit() and 0 < int(port) < 65536 and ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10"):
                    serials.append(parts[0])
            except ValueError:
                pass
        if len(serials) != 1:
            return {"ok": False, "status": "adb_unavailable"}
        serial = serials[0]

        def shell(command):
            return run(["-s", serial, "shell", command])

        # Recheck actual owner preference, not just a potentially delayed heartbeat.
        prefs = ET.fromstring(shell("run-as " + PACKAGE + " cat shared_prefs/z0live_control.xml"))
        if not any(e.tag == "boolean" and e.get("name") == "in_app_control" and e.get("value") == "true" for e in prefs):
            return {"ok": False, "status": "owner_off"}
        raw = shell("settings get secure " + KEY)
        entries = [v for v in raw.split(":") if v and v != "null"]
        if SERVICE in entries:
            return {"ok": True, "status": "already_enabled"}
        entries.append(SERVICE)
        shell("settings put secure " + KEY + " " + shlex.quote(":".join(entries)))
        shell("settings put secure accessibility_enabled 1")
        after = shell("settings get secure " + KEY).split(":")
        master = shell("settings get secure accessibility_enabled")
        ok = master == "1" and all(entry in after for entry in entries)
        return {"ok": ok, "status": "re_enabled" if ok else "verification_failed"}
    except (OSError, subprocess.TimeoutExpired, ET.ParseError):
        return {"ok": False, "status": "adb_unavailable"}


class AccessibilityWatchdog:
    def __init__(self, store, clock=time.time, restore=adb_restore):
        self.store, self.clock, self.restore = store, clock, restore
        self.pending = threading.Lock()
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS accessibility_recovery(
                id INTEGER PRIMARY KEY, device TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 0)""")

    def offer(self, device, body):
        # Do not hold the 2-second presence HTTP response behind ADB. No queue of stale heartbeats.
        if body.get("kind") != "phone" or body.get("in_app_control") is not True or body.get("a11y_enabled") is not False or os.environ.get("Z0LIVE_GESTURE_ADB") != "1":
            return
        if not self.pending.acquire(blocking=False):
            return
        def work():
            try:
                self.heartbeat(device, body)
            finally:
                self.pending.release()
        threading.Thread(target=work, name="z0live-a11y-recovery", daemon=True).start()

    def heartbeat(self, device, body):
        # Missing fields from old clients do not opt them in. Strict booleans only.
        if body.get("kind") != "phone" or body.get("in_app_control") is not True or body.get("a11y_enabled") is not False or os.environ.get("Z0LIVE_GESTURE_ADB") != "1":
            return
        with _LOCK:
            now = self.clock()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # Persist reservation before ADB; rate-limit failures and restart recovery too.
                if db.execute("SELECT 1 FROM accessibility_recovery WHERE created>? LIMIT 1", (now - 60,)).fetchone():
                    return
                receipt = db.execute("INSERT INTO accessibility_recovery(device,created,status) VALUES(?,?,'reserved')", (device, now)).lastrowid
            result = self.restore()
            live_call = None
            announced = False
            with self.store.connect() as db:
                db.execute("UPDATE accessibility_recovery SET status=?,ok=? WHERE id=?", (result["status"], result["ok"], receipt))
                # Scope to the latest, currently human-started session of this credential.
                session = db.execute("SELECT id,ended,updated FROM sessions WHERE device=? AND client='lawnchair' ORDER BY created DESC,rowid DESC LIMIT 1", (device,)).fetchone()
                if result["status"] == "re_enabled" and session and not session["ended"] and session["updated"] > now - 120:
                    # Unique source is durable once-per-session speech across retries/restarts.
                    if self.store._live is not None:
                        live_call = self.store._live.get(session["id"])
                    cursor = db.execute("INSERT OR IGNORE INTO inbox(device,session_id,say,action,params,created,source) VALUES(?,?,?,NULL,'{}',?,?)", (device, session["id"], "" if live_call else "Re-enabled in-app control.", now, "accessibility-recovery:" + session["id"]))
                    announced = cursor.rowcount == 1
            if announced and live_call:
                from . import bidi_protocol as bp
                sent = live_call.append("recovered-accessibility", bp.FINAL_LABEL + " Re-enabled in-app control. Mention this once, briefly, at the next safe voice turn.", "speakable")
                with self.store.connect() as db:
                    db.execute("UPDATE accessibility_recovery SET status=? WHERE id=?", ("re_enabled_voice_context" if sent else "re_enabled_voice_unavailable", receipt))
