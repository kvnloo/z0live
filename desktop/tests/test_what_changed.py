"""What-changed answers report real agent work, not template text or voice telemetry."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from z0live_desktop import work_sources as sources
from z0live_desktop.work_tools import WorkTools


def make_profile(root: Path, name: str, rows):
    db_path = root / "profiles" / name / "state.db"
    db_path.parent.mkdir(parents=True)
    db = sqlite3.connect(db_path)
    db.execute(
        """CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, cwd TEXT, profile_name TEXT,
           last_activity_at REAL, last_activity_description TEXT, message_count INT,
           archived INT, hidden INT)"""
    )
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


class WhatChangedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        now = time.time()
        make_profile(root, "clean", [
            ("s1", "Debug Hermes crashing", "cli", "/repo/hermes", "clean", now - 20, "receiving stream response", 40, 0, 0),
            ("s2", "Plan garden beds", "cli", "/repo/garden_room", "clean", now - 4000, "", 12, 0, 0),
            ("s3", "Audit repo x", "oneshot", "/repo/x", "clean", now - 600, "", 3, 0, 0),
            ("s4", "Audit repo y", "oneshot", "/repo/y", "clean", now - 700, "", 3, 0, 0),
            ("s5", "Subagent: Restyle Dash", "subagent", "/repo/dash", "clean", now - 30, "terminal command running", 9, 0, 0),
            ("s6", "Old archived thing", "cli", "/repo/old", "clean", now - 100, "", 2, 1, 0),
        ])
        # A profile whose database never created a sessions table is not a reportable gap.
        empty = root / "state.db"
        sqlite3.connect(empty).close()
        self.env = patch.dict(os.environ, {"Z0LIVE_HISTORY_ROOTS": str(root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_live_and_earlier_sessions_without_noise(self):
        with patch.object(sources, "projects", return_value={}):
            result = sources.changes(since="today")
        whats = [i["what"] for i in result["items"]]
        self.assertIn("Debug Hermes crashing", whats)
        self.assertIn("Restyle Dash", whats)  # live titled subagent is shown, prefix stripped
        self.assertNotIn("Old archived thing", whats)
        self.assertFalse(any("Audit repo" in w for w in whats))  # background scans are only counted
        self.assertTrue(any(w.startswith("2 background agent runs") for w in whats))
        self.assertFalse(any(i["source"] == "telemetry" for i in result["items"]))
        self.assertEqual(result["gaps"], [])

    def test_speech_is_facts_for_the_model(self):
        with patch.object(sources, "projects", return_value={}):
            spoken = WorkTools.speech(sources.changes(since="today"))
        self.assertTrue(spoken.startswith("Agents working right now: Debug Hermes crashing"))
        self.assertIn("Also worked on earlier: Plan garden beds", spoken)
        self.assertNotIn("observed", spoken)
        self.assertNotIn("More work context", spoken)

    def test_today_just_after_midnight_covers_the_evening(self):
        start, end = sources.window("today")
        self.assertLessEqual(end - start, 36 * 3600)
        self.assertGreaterEqual(end - start, 12 * 3600 - 1)

    def test_empty_period_says_so_plainly(self):
        self.assertEqual(WorkTools.speech({"items": []}),
                         "Nothing new was found in agent sessions or commits for that period.")


if __name__ == "__main__":
    unittest.main()
