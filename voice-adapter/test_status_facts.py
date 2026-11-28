"""Synthetic call replays for unsupported status claims and volatile counts."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mobile_live
import status_facts as facts
from test_replay import delegation, hermes, lane, replay_rows, turn


def snapshot():
    return {
        "t": 20, "call": "fixture", "lanes_available": True, "cron_available": True,
        "asks": [{"id": "a1", "text": "inspect aquarium", "observed": 20,
                  "running": Counter({"sol 6.2": 2}), "done": Counter({"haiku": 1}),
                  "failed": Counter(), "unconfirmed": Counter()}],
        "cron": [],
        "jobs": [],
        "items": [],
    }


class StatusReplay(unittest.TestCase):
    def test_ten_audited_patterns_are_replaced_before_speech(self):
        state = snapshot()
        state["cron"] = [{"id": "cron1", "name": "z0live TV hill climb", "enabled": True,
                          "state": "scheduled", "last_status": "ok", "last_run_at": "2026-01-01T10:00:00+00:00",
                          "next_run_at": "2026-01-01T12:00:00+00:00"}]
        cases = [
            ("The TV hill climb stopped because its cache was unavailable.", "TV hill climb is scheduled"),
            ("I reconnected the blue drive and it is fine.", facts.UNKNOWN),
            ("The backup disk was ejected safely and had no open files.", facts.UNKNOWN),
            ("One Astra lane and two Fable lanes started.", "2 lanes running (2 sol 6.2)"),
            ("The relay bot doesn't run the server and needs a restart.", facts.UNKNOWN),
            ("The relay bot picked up the image request.", facts.UNKNOWN),
            ("I approved 41 updates and rejected 13 edits.", facts.UNKNOWN),
            ("Right now, zero lanes.", "2 lanes running (2 sol 6.2)"),
            ("Every lane has finished.", "2 lanes running (2 sol 6.2)"),
            ("A stuck macOS sandbox daemon caused it.", facts.UNKNOWN),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw), patch.object(facts, "read", return_value=state):
                rows = (turn(5, 9, "user", "Give me an update about the aquarium", "u1")
                        + delegation(9, "d1", "Explain what happened to the aquarium")
                        + turn(9.1, 10, "assistant", "Let me check.", "a1")
                        + hermes("d1", 4, raw))
                run, report = replay_rows(rows)
                output = report["answers"]["d1"]
                self.assertIn(expected, output)
                self.assertNotIn(raw, output)
                self.assertFalse(run.ledger_items[0]["results"])

    def test_direct_status_reads_again_at_provider_handoff(self):
        initial, newer = snapshot(), snapshot()
        newer["asks"][0]["running"]["sol 6.2"] = 1
        newer["asks"][0]["done"]["sol 6.2"] = 1
        with patch.object(facts, "read", side_effect=[initial, newer]):
            rows = (turn(5, 9, "user", "How many lanes are running?", "u1")
                    + delegation(9, "d1", "How many lanes are running?")
                    + turn(9.1, 10, "assistant", "Let me check.", "a1"))
            run, report = replay_rows(rows)
        self.assertFalse(run.submitted)
        self.assertIn("1 lane", report["answers"]["d1"])
        self.assertNotIn("2 lanes running", report["answers"]["d1"])

    def test_fast_tier_claim_is_replaced(self):
        with patch.object(facts, "read", return_value=snapshot()):
            rows = (turn(5, 9, "user", "Give me an update about the aquarium", "u1")
                    + delegation(9, "d1", "Explain what happened to the aquarium")
                    + turn(9.1, 10, "assistant", "Let me check.", "a1")
                    + lane("d1", "fast", 2, "No lanes are running.")
                    + hermes("d1", 8, "The aquarium contains river stones."))
            run, _ = replay_rows(rows)
        output = " ".join(str(payload) for _, payload in run.sent if payload.get("channel") == "speakable")
        self.assertNotIn("No lanes are running", output)
        self.assertIn("2 lanes running", output)

    def test_status_requests_and_commands(self):
        for query in ("What's running?", "Status of hill climbs", "Did the aquarium repair start?", "Is anyone on it?"):
            self.assertTrue(facts.is_status(query), query)
        for command in ("Start the aquarium repair", "How many lanes do I have?", "Check status and then start the job"):
            self.assertFalse(facts.is_status(command), command)

    def test_card_is_not_replayed_after_correction(self):
        card = {"did": "d1", "card": {"summary": "The aquarium heater is switched off."}}
        spoken = {"d1": ["Checking the aquarium heater."], "d2": ["Correction, the aquarium heater is switched on."]}
        self.assertTrue(mobile_live.lane_cards.superseded(card, snapshot(), spoken))
        self.assertFalse(mobile_live.lane_cards.superseded(card, snapshot(), {"d2": ["Correction, the garden lights are on."]}))


class SourceReplay(unittest.TestCase):
    def test_events_update_state_counts_without_crossing_call_scope(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"Z0LIVE_JOBS": tmp, "Z0LIVE_LEDGER": tmp}):
            root = Path(tmp)
            rows = [
                {"ev": "ask.received", "ask": "a1", "call": "fixture", "t": 10},
                {"ev": "lane.start", "ask": "a1", "lane": "one", "model": "sol", "t": 11},
                {"ev": "lane.start", "ask": "a1", "lane": "two", "model": "haiku", "t": 12},
                {"ev": "ask.received", "ask": "a2", "call": "other", "t": 13},
                {"ev": "lane.start", "ask": "a2", "lane": "three", "model": "opus", "t": 14},
            ]
            state = {"now": 11, "started": 9, "asks": []}
            (root / "state.json").write_text(json.dumps(state))
            path = root / "events.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            read = lambda: facts.read("fixture", conductor=root, cron=root / "missing.json")
            initial = read()
            self.assertEqual(initial["asks"][0]["running"], {"sol": 1, "haiku": 1})
            with path.open("a") as stream:
                stream.write(json.dumps({"ev": "lane.failed", "ask": "a1", "lane": "two", "t": 15}) + "\n")
            newer = read()
            self.assertEqual(newer["asks"][0]["running"], {"sol": 1})
            self.assertEqual(newer["asks"][0]["failed"], {"haiku": 1})
            self.assertEqual(newer["asks"][0]["observed"], 15)
            self.assertEqual(len(newer["asks"]), 1)
            self.assertFalse(newer["cron_available"])

    def test_manual_launch_is_not_confirmed_by_matching_schedule(self):
        state = snapshot()
        state["cron"] = [{"id": "cron1", "name": "z0live TV hill climb", "enabled": True, "state": "scheduled"}]
        self.assertEqual(facts.answer("Did the manual TV hill climb start?", state), facts.UNKNOWN)
        self.assertIn("I can't confirm a separate launch.", facts.answer("Did the TV hill climb start?", state))

    def test_live_job_observation_is_read_only_and_refreshes_after_wal_write(self):
        from contextlib import closing
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = root / "kanban.db"
            record = root / "jobs" / "t_fixture" / "job.json"
            record.parent.mkdir(parents=True)
            job = {"id": "t_fixture", "title": "Aquarium repair", "engine": "kanban", "state": "waiting",
                   "receipt": {"kind": "kanban", "ref": "t_fixture"}}
            original = json.dumps(job)
            record.write_text(original)
            with (patch.dict(os.environ, {"Z0LIVE_JOBS": str(record.parent.parent), "HERMES_KANBAN_DB": str(board)}),
                  closing(sqlite3.connect(board)) as db):
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
                db.execute("INSERT INTO tasks VALUES ('t_fixture', 'ready')")
                db.commit()
                self.assertEqual(facts.jobs.list_jobs(refresh=False)[0]["state"], "waiting")
                db.execute("UPDATE tasks SET status='running' WHERE id='t_fixture'")
                db.commit()
                row = facts.jobs.list_jobs(refresh=False)[0]
                self.assertEqual(row["state"], "running")
                self.assertIsInstance(row["observed"], float)
            self.assertEqual(record.read_text(), original)
