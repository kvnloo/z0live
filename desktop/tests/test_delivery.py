"""Hermetic edge-case coverage for offline Hermes notification delivery."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from z0live_desktop.core import Store
from z0live_desktop.delivery import Delivery


HEADER = "z0live work updates\n\n"
SUCCESS = subprocess.CompletedProcess(
    args=["hermes"], returncode=0, stdout='{"success":true}'
)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("fixture"))
        with patch(
            "z0live_desktop.core.mint",
            return_value={"value": "mock", "expires_at": 1},
        ):
            self.sid = self.store.new_session(self.device, {"client": "web"})[
                "session_id"
            ]
        self.store.end(self.device, self.sid)

        # A pre-existing inbox row must not be replayed: Delivery snapshots its id.
        self.store.publish("already seen", sid=self.sid, source="lookup:seed")
        self.delivery = Delivery(self.store)
        self.initial_cursor = self.cursor()

        env = patch.dict(os.environ, {"Z0LIVE_TELEGRAM_TARGET": "t"})
        env.start()
        self.addCleanup(env.stop)
        send_patch = patch(
            "z0live_desktop.delivery.subprocess.run", return_value=SUCCESS
        )
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)

    def cursor(self):
        with self.store.connect() as db:
            return int(
                db.execute(
                    "SELECT value FROM meta WHERE key='telegram_cursor'"
                ).fetchone()[0]
            )

    def tick(self):
        self.delivery.next_check = 0
        self.delivery.tick()

    def test_no_target_is_noop(self):
        with patch.dict(os.environ, {"Z0LIVE_TELEGRAM_TARGET": ""}):
            self.delivery.next_check = 0
            with patch.object(
                self.store,
                "connect",
                side_effect=AssertionError("no-target tick queried the database"),
            ):
                self.delivery.tick()
        self.send.assert_not_called()
        self.assertEqual(self.delivery.next_check, 0)
        self.assertEqual(self.cursor(), self.initial_cursor)

    def test_throttle_60s(self):
        self.store.publish("new work", sid=self.sid, source="lookup:work")
        with patch("z0live_desktop.delivery.time.monotonic", return_value=100.0):
            self.tick()
            self.assertEqual(self.delivery.next_check, 160.0)
            with patch.object(
                self.store,
                "connect",
                side_effect=AssertionError("throttled tick queried the database"),
            ):
                # Intentionally do not reset next_check for the second tick.
                self.delivery.tick()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs["input"], HEADER + "new work")

    def test_filters_sources(self):
        self.store.publish("lookup a", sid=self.sid, source="lookup:a")
        self.store.publish("progress b", sid=self.sid, source="task-progress:b")
        self.store.publish("delegation c", sid=self.sid, source="delegation:c")
        self.store.publish("housekeeping d", sid=self.sid, source="housekeeping:d")
        last_id = self.store.publish("unattributed", sid=self.sid, source=None)
        self.tick()
        self.send.assert_called_once()
        payload = self.send.call_args.kwargs["input"]
        self.assertTrue(payload.startswith(HEADER))
        self.assertEqual(
            payload,
            HEADER + "lookup a\n\nprogress b\n\ndelegation c\n\nunattributed",
        )
        self.assertNotIn("housekeeping d", payload)
        self.assertNotIn("already seen", payload)
        self.assertEqual(self.cursor(), last_id)

    def test_run_source_job_kinds(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO jobs(kind,session_id,text,created,run_id)"
                " VALUES(?,?,?,?,?)",
                ("task", self.sid, "fixture", 1.0, "r1"),
            )
            db.execute(
                "INSERT INTO jobs(kind,session_id,text,created,run_id)"
                " VALUES(?,?,?,?,?)",
                ("summary", self.sid, "fixture", 1.0, "r2"),
            )
        self.store.publish("allowed task", sid=self.sid, source="run:r1")
        last_id = self.store.publish(
            "filtered summary", sid=self.sid, source="run:r2"
        )
        self.tick()
        self.send.assert_called_once()
        self.assertEqual(
            self.send.call_args.kwargs["input"], HEADER + "allowed task"
        )
        self.assertEqual(self.cursor(), last_id)

    def test_failure_keeps_cursor_for_retry(self):
        last_id = self.store.publish(
            "retry this work", sid=self.sid, source="lookup:retry"
        )
        failures = (
            (
                "nonzero_returncode",
                subprocess.CompletedProcess(
                    args=["hermes"], returncode=1, stdout='{"success":true}'
                ),
            ),
            (
                "explicit_failure",
                subprocess.CompletedProcess(
                    args=["hermes"], returncode=0, stdout='{"success":false}'
                ),
            ),
            (
                "invalid_json",
                subprocess.CompletedProcess(
                    args=["hermes"], returncode=0, stdout="not-json"
                ),
            ),
            ("timeout", subprocess.TimeoutExpired(cmd=["hermes"], timeout=25)),
        )
        for name, outcome in failures:
            with self.subTest(failure=name):
                self.send.reset_mock(side_effect=True)
                if isinstance(outcome, Exception):
                    self.send.side_effect = outcome
                else:
                    self.send.return_value = outcome
                self.tick()
                self.send.assert_called_once()
                self.assertEqual(
                    self.send.call_args.kwargs["input"], HEADER + "retry this work"
                )
                self.assertEqual(self.cursor(), self.initial_cursor)

        self.send.reset_mock(side_effect=True)
        self.send.return_value = SUCCESS
        self.tick()
        self.send.assert_called_once()
        self.assertEqual(
            self.send.call_args.kwargs["input"], HEADER + "retry this work"
        )
        self.assertEqual(self.cursor(), last_id)

    def test_no_messages_still_advances_cursor(self):
        self.store.publish("maintenance one", sid=self.sid, source="housekeeping:a")
        last_id = self.store.publish(
            "maintenance two", sid=self.sid, source="housekeeping:b"
        )
        self.tick()
        self.send.assert_not_called()
        self.assertEqual(self.cursor(), last_id)

    def test_truncates_payload(self):
        self.store.publish("X" * 13000, sid=self.sid, source="lookup:long")
        self.tick()
        self.send.assert_called_once()
        payload = self.send.call_args.kwargs["input"]
        self.assertEqual(len(payload), len(HEADER) + 12000)
        self.assertEqual(payload, HEADER + "X" * 12000)

    def test_hermes_home_env(self):
        self.store.publish("work", sid=self.sid, source="lookup:env")
        with patch.dict(os.environ, {"Z0LIVE_DELIVERY_HOME": "/tmp/x"}):
            self.tick()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs["env"]["HERMES_HOME"], "/tmp/x")


if __name__ == "__main__":
    unittest.main()
