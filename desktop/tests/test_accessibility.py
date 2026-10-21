"""Offline recovery receipts and fake ADB only; never contacts a handset."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.accessibility import SERVICE, adb_restore
from z0live_desktop.core import Store


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("phone"))
        self.watch = self.store.gestures.accessibility
        self.now = 1000
        self.watch.clock = lambda: self.now
        self.calls = []
        self.watch.restore = lambda: (
            self.calls.append(1) or {"ok": True, "status": "re_enabled"}
        )
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO sessions(id,device,client,created,updated,context,tools,ended) VALUES('session',?,'lawnchair',1000,1000,'{}','[]',0)",
                (self.device,),
            )

    def body(self, **kwargs):
        return dict(kind="phone", in_app_control=True, a11y_enabled=False, **kwargs)

    def test_fail_closed_and_once_per_session_receipt(self):
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "0"}):
            self.watch.heartbeat(self.device, self.body())
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            for body in (
                {"kind": "phone"},
                {"kind": "desktop", "in_app_control": True, "a11y_enabled": False},
                {"kind": "phone", "in_app_control": False, "a11y_enabled": False},
                {"kind": "phone", "in_app_control": True, "a11y_enabled": True},
            ):
                self.watch.heartbeat(self.device, body)
            self.assertEqual(self.calls, [])
            self.watch.heartbeat(self.device, self.body())
            self.watch.heartbeat(self.device, self.body())
            self.now += 61
            self.watch.heartbeat(self.device, self.body())
        self.assertEqual(len(self.calls), 2)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM accessibility_recovery WHERE status='re_enabled' AND ok=1"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM inbox WHERE say='Re-enabled in-app control.'"
                ).fetchone()[0],
                1,
            )

    def test_failure_and_ended_sessions_are_silent(self):
        self.watch.restore = lambda: {"ok": False, "status": "adb_unavailable"}
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            self.watch.heartbeat(self.device, self.body())
            self.now += 61
            self.watch.restore = lambda: {"ok": True, "status": "re_enabled"}
            with self.store.connect() as db:
                db.execute("UPDATE sessions SET ended=1")
            self.watch.heartbeat(self.device, self.body())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_bidi_context_once_without_duplicate_inbox_speech(self):
        call = Mock()
        call.append.return_value = True
        self.store._live = Mock()
        self.store._live.get.return_value = call
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            self.watch.heartbeat(self.device, self.body())
            self.now += 61
            self.watch.heartbeat(self.device, self.body())
        call.append.assert_called_once()
        self.assertEqual(call.append.call_args.args[2], "speakable")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT say FROM inbox").fetchone()[0], "")
            self.assertEqual(
                db.execute(
                    "SELECT status FROM accessibility_recovery ORDER BY id LIMIT 1"
                ).fetchone()[0],
                "re_enabled_voice_context",
            )

    def test_presence_offer_is_nonblocking_and_drops_backlog(self):
        entered, release = threading.Event(), threading.Event()

        def restore():
            entered.set()
            release.wait(2)
            return {"ok": True, "status": "already_enabled"}

        self.watch.restore = restore
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            self.watch.offer(self.device, self.body())
            self.assertTrue(entered.wait(1))
            self.watch.offer(self.device, self.body())
            release.set()
            self.assertTrue(self.watch.pending.acquire(timeout=2))
            self.watch.pending.release()
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM accessibility_recovery").fetchone()[0],
                1,
            )

    def test_presence_validates_optional_booleans(self):
        for field in ("in_app_control", "a11y_enabled"):
            with self.assertRaises(ValueError):
                self.store.gestures.activity(
                    self.device,
                    dict(
                        kind="phone",
                        activity_ts=0,
                        available=True,
                        ready=True,
                        **{field: "true"},
                    ),
                )

    def fake(
        self, devices="100.117.226.39:42409\tdevice", opted=True, fail_write=False
    ):
        calls = []
        state = ["other.pkg/other.Service"]

        def runner(argv, **kwargs):
            calls.append(argv)
            self.assertNotIn("ADB_SERVER_SOCKET", kwargs["env"])
            cmd = argv[-1]
            if argv == ["adb", "devices"]:
                out = "List of devices attached\n" + devices + "\n"
            elif cmd.startswith("run-as"):
                out = (
                    '<map><boolean name="in_app_control" value="'
                    + str(opted).lower()
                    + '" /></map>'
                )
            elif cmd == "settings get secure accessibility_enabled":
                out = "1"
            elif cmd.startswith("settings get"):
                out = state[0]
            elif cmd.startswith("settings put secure enabled_accessibility_services"):
                if not fail_write:
                    state[0] += ":" + SERVICE
                out = ""
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        return runner, calls, state

    def test_merge_readback_and_owner_off(self):
        runner, calls, state = self.fake()
        with patch.dict(os.environ, {"ADB_SERVER_SOCKET": "unrelated"}):
            self.assertEqual(adb_restore(runner)["status"], "re_enabled")
        self.assertEqual(state[0], "other.pkg/other.Service:" + SERVICE)
        self.assertEqual(adb_restore(runner)["status"], "already_enabled")
        runner, calls, _ = self.fake(opted=False)
        self.assertEqual(adb_restore(runner)["status"], "owner_off")
        self.assertFalse(any("settings put" in c[-1] for c in calls))
        runner, _, _ = self.fake(fail_write=True)
        self.assertEqual(adb_restore(runner)["status"], "verification_failed")

    def test_never_guess_devices_or_use_usb(self):
        for devices in (
            "",
            "USB\tdevice",
            "100.1.1.1:1\tdevice",
            "100.64.1.1:1\tunauthorized",
            "100.64.1.1:1\tdevice\n100.64.1.2:2\tdevice",
        ):
            runner, calls, _ = self.fake(devices)
            self.assertEqual(adb_restore(runner)["status"], "adb_unavailable")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
