"""Hermetic unit tests for desktop work-source pure helpers."""

from datetime import datetime, timedelta
from pathlib import Path
import sys
import json
import os
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import work_sources as sources


class WorkSourcesHelpersTests(unittest.TestCase):
    def test_compact_redacts_credentials(self):
        authorization = sources.compact("Authorization: Bearer abc.def")
        self.assertIn("Bearer [REDACTED]", authorization)
        self.assertNotIn("abc.def", authorization)
        self.assertEqual(
            sources.compact("api_key=FAKEVALUE123 next"),
            "api_key=[REDACTED] next",
        )
        self.assertEqual(
            sources.compact("API-KEY: x1"), "API-KEY: [REDACTED]"
        )
        self.assertEqual(sources.compact("sk-" + "A" * 20), "[REDACTED]")
        self.assertEqual(sources.compact("ghp_" + "b" * 16), "[REDACTED]")
        self.assertEqual(sources.compact("sk-short"), "sk-short")

    def test_compact_redacts_quoted_json_credentials(self):
        value = {
            "api_key": "synthetic key with spaces",
            "nested": {"TOKEN": 'synthetic"escaped\\value'},
            "password": "synthetic pass",
            "secret": "synthetic secret",
            "label": "keep this text",
        }
        expected = {
            "api_key": "[REDACTED]",
            "nested": {"TOKEN": "[REDACTED]"},
            "password": "[REDACTED]",
            "secret": "[REDACTED]",
            "label": "keep this text",
        }
        for source in (value, json.dumps(value)):
            with self.subTest(source_type=type(source).__name__):
                self.assertEqual(json.loads(sources.compact(source)), expected)

    def test_compact_non_string_and_limit(self):
        self.assertEqual(sources.compact({"a": "é"}), '{"a": "é"}')
        self.assertEqual(len(sources.compact("x" * 700)), 600)
        self.assertEqual(len(sources.compact("x" * 50, limit=10)), 10)

    def test_window(self):
        before = datetime.now().astimezone()
        start, end = sources.window("today")
        after = datetime.now().astimezone()
        possible_midnights = {
            moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for moment in (before, after)
        }
        self.assertIn(start, possible_midnights)
        self.assertGreaterEqual(end, before.timestamp())
        self.assertLessEqual(end, after.timestamp())

        before = datetime.now().astimezone()
        yesterday_start, yesterday_end = sources.window("yesterday")
        after = datetime.now().astimezone()
        possible_midnights = {
            moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for moment in (before, after)
        }
        self.assertIn(yesterday_end, possible_midnights)
        self.assertIn(yesterday_end - yesterday_start, (82800, 86400, 90000))

        week_start, week_end = sources.window("week")
        self.assertAlmostEqual(week_end - week_start, 7 * 86400, delta=3600)

        with self.assertRaises(ValueError) as error:
            sources.window("month")
        self.assertEqual(
            str(error.exception), "since must be today, yesterday or week"
        )

    @unittest.skipUnless(hasattr(time, "tzset"), "requires local timezone control")
    def test_window_uses_each_local_dates_dst_offset(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                for now, hours in ((datetime(2026, 3, 9, 12), 23),
                                   (datetime(2026, 11, 2, 12), 25)):
                    with self.subTest(now=now), patch.object(sources, "datetime", wraps=datetime) as clock:
                        clock.now.return_value = now
                        start, end = sources.window("yesterday")
                        midnight = now.replace(hour=0)
                        previous = midnight.replace(day=midnight.day - 1)
                        self.assertEqual(start, previous.timestamp())
                        self.assertEqual(end, midnight.timestamp())
                        self.assertEqual(end - start, hours * 3600)
                        week_start, week_end = sources.window("week")
                        self.assertEqual(week_start, (now - timedelta(days=7)).timestamp())
                        self.assertEqual(week_end, now.timestamp())
        finally:
            time.tzset()

    def test_in_window(self):
        self.assertTrue(sources.in_window(10, 10, 20))
        self.assertFalse(sources.in_window(20, 10, 20))
        self.assertTrue(sources.in_window("1970-01-01T00:00:10Z", 10, 20))
        self.assertFalse(sources.in_window("1970-01-01T00:00:20Z", 10, 20))
        self.assertFalse(sources.in_window("garbage", 0, 1))
        self.assertFalse(sources.in_window(None, 0, 1))

    def test_terms(self):
        self.assertEqual(
            sources.terms("What was I working on in z0live yesterday?"),
            ["z0live"],
        )
        self.assertEqual(
            sources.terms("alpha, beta! gamma?"), ["alpha", "beta", "gamma"]
        )
        candidates = [f"term{i}" for i in range(15)]
        self.assertEqual(sources.terms(" ".join(candidates)), candidates[:12])

    def test_project_name(self):
        registry = {
            "garden_room": "/x",
            "z0live": "/y",
            "lawnchair": "/z",
        }
        with patch(
            "z0live_desktop.work_sources.projects", return_value=registry
        ) as projects:
            self.assertIsNone(sources.project_name(None))
            projects.assert_not_called()
            for value in ("Garden Room", "garden", "gardenroom"):
                with self.subTest(alias=value):
                    self.assertEqual(sources.project_name(value), "garden_room")
            self.assertEqual(
                sources.project_name("lawnchair-z0live"), "lawnchair"
            )
            with self.assertRaises(ValueError) as error:
                sources.project_name("nope")
            self.assertTrue(str(error.exception).startswith("unknown project"))
            for value in (5, "x" * 101):
                with self.subTest(invalid=value):
                    with self.assertRaises(ValueError) as error:
                        sources.project_name(value)
                    self.assertEqual(str(error.exception), "invalid project")


if __name__ == "__main__":
    unittest.main()
