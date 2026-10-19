"""Synthetic deterministic SQLite/JSONL event replay and crash-window controls.

This suite does not run a live broker, provider, Hermes worker, or phone.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import core
from z0live_desktop.core import Store

FIXTURE = Path(__file__).parent / "fixtures" / "event-replay-cases.json"


class EventReplayConformance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.temporary.name)
        self.device = self.store.authenticate(self.store.pair("synthetic-primary-device"))
        self.other_device = self.store.authenticate(self.store.pair("synthetic-other-device"))
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with patch.object(core, "mint", return_value={
            "value": "synthetic-mint-placeholder", "expires_at": 1,
        }) as mint:
            self.primary = self.store.new_session(
                self.device, {"client": "web"})["session_id"]
            self.secondary = self.store.new_session(
                self.device, {"client": "web"})["session_id"]
            self.assertEqual(mint.call_count, 2)

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, names, sid=None):
        sid = self.primary if sid is None else sid
        return [{"session_id": sid, **self.fixture["events"][name]} for name in names]

    def db_events(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,session_id,ts,body,exported FROM events ORDER BY id"
            )]

    def export_path(self, sid=None):
        return (Path(self.temporary.name) / "sessions" /
                ((self.primary if sid is None else sid) + ".jsonl"))

    def jsonl_events(self, sid=None):
        path = self.export_path(sid)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_event_ids(self, expected):
        self.assertEqual(
            [json.loads(row["body"])["data"]["event_id"] for row in self.db_events()],
            expected,
        )

    def test_versioned_fixture_atomicity_deduplication_and_order(self):
        self.assertEqual(self.fixture["fixture_revision"], 1)
        self.assertIn("Entirely synthetic", self.fixture["provenance"])
        self.assertEqual(
            [case["id"] for case in self.fixture["cases"]],
            ["reject-mixed-atomic", "accept-batch", "exact-retry",
             "identity-scoped-to-other-session"],
        )
        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                sid = self.secondary if case["session"] == "secondary" else self.primary
                events = self.build(case["sequence"], sid)
                if case["error"]:
                    self.assertEqual(case["error"], "ValueError")
                    with self.assertRaises(ValueError):
                        self.store.add_events(self.device, events)
                else:
                    result = self.store.add_events(self.device, events)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["accepted"], case["accepted"])
                self.assertEqual(len(self.db_events()), case["canonical_count"])
                self.assert_event_ids(case["event_order"])
                self.assertEqual(
                    len(self.jsonl_events(self.primary)) +
                    len(self.jsonl_events(self.secondary)),
                    case["canonical_count"],
                )
        self.assertEqual(
            [line["data"]["event_id"] for line in self.jsonl_events(self.primary)],
            ["synthetic-alpha-v1", "synthetic-call-v1"],
        )
        self.assertEqual(
            [line["data"]["event_id"] for line in self.jsonl_events(self.secondary)],
            ["synthetic-alpha-v1"],
            "Same event_id in another owned session is not a duplicate",
        )

    def test_same_identity_changed_body_and_reopen_dedupe(self):
        self.assertEqual(
            self.store.add_events(self.device, self.build(["first"]))["accepted"], 1
        )
        tampered = self.build(["first"])
        tampered[0] = {**tampered[0], "text": "synthetic changed retry body"}
        self.assertEqual(self.store.add_events(self.device, tampered)["accepted"], 0)
        self.assertEqual(self.jsonl_events()[0]["text"], "synthetic transcript alpha")
        self.store = Store(self.temporary.name)  # Reopen canonical SQLite; no mint.
        self.assertEqual(self.store.add_events(
            self.device, self.build(["first"]))["accepted"], 0)
        self.assertEqual(len(self.db_events()), 1)
        self.assertEqual(len(self.jsonl_events()), 1)

    def test_atomic_bad_middle_event_and_invalid_session(self):
        self.store.add_events(self.device, self.build(["first"]))
        before = self.db_events()
        before_file = self.export_path().read_bytes()
        with self.assertRaises(ValueError):
            self.store.add_events(self.device, self.build(["second", "invalid", "later"]))
        self.assertEqual(self.db_events(), before)
        self.assertEqual(self.export_path().read_bytes(), before_file)
        with self.assertRaises(LookupError):
            self.store.add_events(self.other_device, self.build(["second"]))
        with self.assertRaises(LookupError):
            self.store.add_events(self.device, self.build(["later"], "missing-session"))
        self.assertEqual(self.db_events(), before)
        self.assertEqual(
            self.store.add_events(self.device, self.build(["second"]))["accepted"], 1
        )
        self.assert_event_ids(["synthetic-alpha-v1", "synthetic-call-v1"])

    def test_session_end_rejects_new_events_without_deletion(self):
        self.store.add_events(self.device, self.build(["first"]))
        self.assertTrue(self.store.end(self.device, self.primary)["ok"])
        self.assertTrue(self.store.end(self.device, self.primary)["ok"])
        before = self.db_events()
        with self.assertRaisesRegex(ValueError, "session has ended"):
            self.store.add_events(self.device, self.build(["later"]))
        self.assertEqual(self.db_events(), before)
        self.assertEqual(len(self.jsonl_events()), 1)

    def test_producer_timestamp_is_not_receipt_timestamp(self):
        self.store.add_events(self.device, self.build(["first"]))
        row = self.db_events()[0]
        event = json.loads(row["body"])
        self.assertEqual(event["ts"], 1234.125)  # Controlled producer value.
        self.assertGreater(row["ts"], 1234.125)  # SQLite reception clock.
        self.assertEqual(self.jsonl_events()[0]["ts"], 1234.125)
        self.assertEqual(self.jsonl_events()[0]["id"], row["id"])

    def test_partial_jsonl_tail_and_unexported_checkpoint_repair(self):
        self.store.add_events(self.device, self.build(["first", "second"]))
        path = self.export_path()
        original = path.read_bytes()
        with path.open("ab") as file:
            file.write(b'{"synthetic_truncated_tail":')
        with self.store.connect() as db:
            db.execute("UPDATE events SET exported=0 WHERE session_id=?",
                       (self.primary,))
        self.store.export_events()
        self.assertEqual(path.read_bytes(), original,
                         "Partial trailing line removed; already-exported ids not duplicated")
        self.assertEqual(len(self.jsonl_events()), 2)
        self.assertTrue(all(row["exported"] == 1 for row in self.db_events()))
        self.assertEqual(self.store.add_events(
            self.device, self.build(["later"]))["accepted"], 1)
        self.assert_event_ids(
            ["synthetic-alpha-v1", "synthetic-call-v1", "synthetic-state-v1"]
        )

    def test_missing_jsonl_rebuilt_from_canonical_sqlite_on_reopen(self):
        self.store.add_events(self.device, self.build(["first", "second"]))
        expected = self.jsonl_events()
        self.export_path().unlink()  # Deliberate synthetic checkpoint loss.
        with self.store.connect() as db:
            db.execute("UPDATE events SET exported=0 WHERE session_id=?",
                       (self.primary,))
        self.store = Store(self.temporary.name)
        self.assertEqual(self.jsonl_events(), expected)
        self.assertEqual(len(self.db_events()), 2)
        self.assertTrue(all(row["exported"] == 1 for row in self.db_events()))

    def test_batch_size_and_schema_boundaries_leave_no_partial_events(self):
        items = self.build(["first"])
        with self.assertRaisesRegex(ValueError, "1..100"):
            self.store.add_events(self.device, [])
        oversized = [{
            "session_id": self.primary, "type": "user_transcript",
            "text": "synthetic " + str(i),
            "data": {"event_id": "synthetic-bulk-" + str(i)},
        } for i in range(101)]
        with self.assertRaisesRegex(ValueError, "1..100"):
            self.store.add_events(self.device, oversized)
        with self.assertRaises(ValueError):
            self.store.add_events(self.device, items + [{
                "session_id": self.primary, "type": "app_state", "data": [],
            }])
        self.assertEqual(self.db_events(), [])
        self.assertEqual(self.store.add_events(self.device, items)["accepted"], 1)
        self.assertEqual(len(self.jsonl_events()), 1)


if __name__ == "__main__":
    unittest.main()
