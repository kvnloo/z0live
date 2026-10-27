"""Hermetic unit tests for desktop work-source pure helpers."""

from datetime import datetime
from pathlib import Path
import sys
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
