"""Offline HTTP + fake-ADB verification. Never invokes a real adb executable or phone."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store
from z0live_desktop.gestures import adb_launch
from z0live_desktop.server import Handler, Server


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.tokens = [self.store.pair(x) for x in ("source", "phone", "desktop")]
        self.source, self.phone, self.desktop = [
            self.store.authenticate(t) for t in self.tokens
        ]
        self.g = self.store.gestures
        self.now = time.time()
        self.g.clock = lambda: self.now
        self.g.listener(
            self.source, dict(source_device=self.source, enabled=True, ttl_seconds=120)
        )

    def presence(self, device, kind, at=None, ready=True):
        self.g.activity(
            device,
            dict(
                kind=kind,
                activity_ts=self.now if at is None else at,
                available=True,
                ready=ready,
            ),
        )

    def event(self, **extra):
        return dict(
            gesture="double_tap",
            source_device=self.source,
            ts=self.now,
            nonce=str(uuid.uuid4()),
            **extra,
        )

    def test_tie_phone_latest_desktop_and_pin(self):
        self.presence(self.phone, "phone")
        self.presence(self.desktop, "desktop")
        self.assertEqual(self.g.submit(self.source, self.event())["target"], "phone")
        self.now += 1
        self.presence(self.desktop, "desktop")
        self.assertEqual(self.g.submit(self.source, self.event())["target"], "desktop")
        self.g.pin(self.phone, dict(ttl_seconds=2))
        self.assertEqual(self.g.submit(self.source, self.event())["target"], "phone")
        self.now += 3
        self.assertEqual(self.g.submit(self.source, self.event())["target"], "desktop")

    def test_listener_conflict_identity_expiry(self):
        with self.assertRaises(ValueError):
            self.g.listener(
                self.phone,
                dict(source_device=self.phone, enabled=True, ttl_seconds=120),
            )
        with self.assertRaises(ValueError):
            self.g.submit(self.phone, self.event())
        self.now += 121
        with self.assertRaises(ValueError):
            self.g.submit(self.source, self.event())

    def test_nonce_persistent_concurrent_and_at_most_once(self):
        self.presence(self.phone, "phone")
        event = self.event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: self.g.submit(self.source, event), range(8))
            )
        self.assertEqual(len({r["command_id"] for r in results}), 1)
        reopened = Store(self.tmp.name).gestures
        self.assertTrue(reopened.submit(self.source, event)["duplicate"])
        self.assertEqual(len(self.g.commands(self.phone)["commands"]), 1)
        self.assertEqual(self.g.commands(self.phone)["commands"], [])
        self.assertEqual(self.g.commands(self.desktop)["commands"], [])

    def test_freshness_validation_and_no_target(self):
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "0"}):
            self.assertFalse(self.g.submit(self.source, self.event())["ok"])
        for ts in (self.now - 2, self.now + 2, float("nan"), True):
            event = self.event()
            event["ts"] = ts
            with self.assertRaises(ValueError):
                self.g.submit(self.source, event)
        for nonce in ("BAD", str(uuid.uuid4()).upper()):
            event = self.event()
            event["nonce"] = nonce
            with self.assertRaises(ValueError):
                self.g.submit(self.source, event)

    def test_triple_tap_expiry_and_ack_ownership(self):
        self.presence(self.phone, "phone")
        event = self.event()
        event["gesture"] = "triple_tap"
        result = self.g.submit(self.source, event)
        self.assertEqual(
            self.g.commands(self.phone)["commands"][0]["gesture"], "triple_tap"
        )
        self.assertFalse(
            self.g.ack(self.desktop, dict(id=result["command_id"], status="ended"))[
                "ok"
            ]
        )
        self.assertTrue(
            self.g.ack(self.phone, dict(id=result["command_id"], status="ended"))["ok"]
        )
        self.g.submit(self.source, self.event())
        self.now += 4
        self.assertEqual(self.g.commands(self.phone)["commands"], [])

    def test_stale_and_revoked_target(self):
        self.presence(self.phone, "phone")
        self.store.revoke_token(self.tokens[1])
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "0"}):
            self.assertFalse(self.g.submit(self.source, self.event())["ok"])
            self.presence(self.desktop, "desktop")
            self.now += 16
            self.assertFalse(self.g.submit(self.source, self.event())["ok"])

    def test_adb_opt_in_and_unavailable(self):
        self.presence(self.phone, "phone", ready=False)
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "0"}):
            self.assertEqual(
                self.g.submit(self.source, self.event())["status"], "phone_not_ready"
            )
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            self.g.adb = lambda _: {"ok": False, "status": "adb_unavailable"}
            self.assertFalse(self.g.submit(self.source, self.event())["ok"])

    def live_call(self, updated=None, ended=0):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO sessions(id,device,client,created,updated,context,tools,ended) "
                "VALUES(?,?,'lawnchair',?,?,'{}','[]',?)",
                (str(uuid.uuid4()), self.phone, self.now, self.now if updated is None else updated, ended),
            )

    def fake_adb(self):
        calls = []

        def adb(gesture):
            calls.append(gesture)
            return {"ok": True, "status": "activity_dispatched", "dispatch_ms": 1.0}

        self.g.adb = adb
        return calls

    def test_adb_fallback_without_presence(self):
        calls = self.fake_adb()
        # Opt-out keeps the old fail-closed answer and never touches adb.
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "0"}):
            self.assertEqual(
                self.g.submit(self.source, self.event())["status"], "no_active_target"
            )
        self.assertEqual(calls, [])
        # A dozing Standby: its last heartbeat is older than the 15 s window.
        self.presence(self.phone, "phone", ready=False)
        self.now += 16
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            event = self.event()
            result = self.g.submit(self.source, event)
            self.assertEqual(result["status"], "activity_dispatched")
            self.assertEqual(result["fallback"], "no_presence")
            self.assertEqual(result["target"], "phone")
            # The receipt is rewritten after dispatch; a replay never re-launches.
            again = self.g.submit(self.source, event)
            self.assertTrue(again["duplicate"])
            self.assertEqual(again["status"], "activity_dispatched")
            # Nothing is queued for a surface that is not polling.
            self.assertEqual(self.g.commands(self.phone)["commands"], [])
        self.assertEqual(calls, ["double_tap"])
        # The listener lease is still required for the fallback.
        self.now += 200
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            with self.assertRaises(ValueError):
                self.g.submit(self.source, self.event())
        self.assertEqual(calls, ["double_tap"])

    def test_owner_gesture_mapping(self):
        """double + no call = start, double in a call = mute toggle, triple = end."""
        calls = self.fake_adb()

        def send(gesture):
            event = self.event()
            event["gesture"] = gesture
            return self.g.submit(self.source, event)

        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            # No call, no presence: a triple has nothing to end and must not relaunch.
            self.assertEqual(send("triple_tap")["status"], "no_call_to_end")
            # No call, no presence: a double starts one through the adb relaunch.
            self.assertEqual(send("double_tap")["status"], "activity_dispatched")
            # A second double inside 3 s (a false positive, or the call still minting) is dropped.
            self.now += 1
            self.assertEqual(send("double_tap")["status"], "adb_debounced")
            self.assertEqual(calls, ["double_tap"])
            # In a call without presence a double must not relaunch: that would toggle mute blind.
            self.now += 5
            self.live_call()
            self.assertEqual(send("double_tap")["status"], "session_live_no_presence")
            # ...but a triple still ends the call, through the relaunch's explicit end extra.
            self.assertEqual(send("triple_tap")["status"], "activity_dispatched")
            self.assertEqual(calls, ["double_tap", "triple_tap"])
            # In a call WITH presence both go to the live session as commands, never adb.
            self.now += 5
            self.presence(self.phone, "phone")
            double, triple = send("double_tap"), send("triple_tap")
            self.assertEqual((double["status"], triple["status"]), ("queued", "queued"))
            queued = [c["gesture"] for c in self.g.commands(self.phone)["commands"]]
            self.assertEqual(queued, ["double_tap", "triple_tap"])
            self.assertEqual(len(calls), 2)
            # A call whose telemetry stopped two minutes ago is not live; a double starts again.
            self.now += 200
            self.g.listener(
                self.source, dict(source_device=self.source, enabled=True, ttl_seconds=120)
            )
            self.assertEqual(send("double_tap")["status"], "activity_dispatched")
            self.assertEqual(calls[-1], "double_tap")

    def test_adb_argv_runtime_resolution_and_down(self):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            out = (
                "List of devices attached\n100.64.1.2:4567\tdevice\nUSBTEST\tdevice\n"
                if argv == ["adb", "devices"]
                else "Starting: Intent"
            )
            return subprocess.CompletedProcess(argv, 0, out, "")

        self.assertTrue(adb_launch("double_tap", fake)["ok"])
        self.assertEqual(
            calls[1],
            [
                "adb",
                "-s",
                "100.64.1.2:4567",
                "shell",
                "am",
                "start",
                "-n",
                "app.lawnchair.play.debug/app.lawnchair.zer0.voice.z0live.Z0LiveActivity",
                "--ez",
                "autostart",
                "true",
            ],
        )
        self.assertTrue(adb_launch("triple_tap", fake)["ok"])
        self.assertEqual(calls[-1][-3:], ["--es", "gesture", "triple_tap"])
        for output in (
            "List of devices attached\n",
            "List of devices attached\n100.1.1.1:1\tunauthorized\n",
            "List of devices attached\n100.1.1.1:1\tdevice\n100.2.2.2:2\tdevice\n",
        ):
            self.assertFalse(
                adb_launch(
                    "double_tap",
                    lambda a, **k: subprocess.CompletedProcess(a, 0, output, ""),
                )["ok"]
            )

        def down(*a, **k):
            raise FileNotFoundError()

        self.assertFalse(adb_launch("double_tap", down)["ok"])

        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("fake", 2)

        self.assertFalse(adb_launch("double_tap", timeout)["ok"])

    def test_http_auth_and_injected_adb_latency(self):
        server = Server(("127.0.0.1", 0), Handler)
        server.store = self.store
        server.origins = set()
        server.stop = threading.Event()
        server.sse_lock = threading.Lock()
        server.sse_clients = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url + "/v1/devices/self")
        self.assertEqual(denied.exception.code, 401)
        self.presence(self.phone, "phone", ready=False)
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            time.sleep(0.005)
            return subprocess.CompletedProcess(
                argv,
                0,
                "List of devices attached\n100.64.1.2:4567\tdevice\n"
                if len(argv) == 2
                else "Starting: Intent",
                "",
            )

        self.g.adb = lambda gesture: adb_launch(gesture, fake)
        samples = []
        with patch.dict(os.environ, {"Z0LIVE_GESTURE_ADB": "1"}):
            for _ in range(5):
                self.now += 4  # past the one-relaunch-per-3-s debounce
                body = json.dumps(self.event()).encode()
                request = urllib.request.Request(
                    url + "/v1/gesture",
                    data=body,
                    headers={
                        "Authorization": "Bearer " + self.tokens[0],
                        "Content-Type": "application/json",
                    },
                )
                start = time.perf_counter()
                result = json.load(urllib.request.urlopen(request))
                samples.append(round((time.perf_counter() - start) * 1000, 2))
                self.assertEqual(result["status"], "activity_dispatched")
        self.assertEqual(len(calls), 10)
        print("HTTP -> injected fake ADB dispatch ms (not phone/audio):", samples)


if __name__ == "__main__":
    unittest.main()
