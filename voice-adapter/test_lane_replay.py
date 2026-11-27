"""Offline checks for bench/lane_replay.py: the live-lanes rebuild and the U3 score."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))
import lane_replay as L  # noqa: E402


class LiveAtTests(unittest.TestCase):
    def test_running_and_finished_at_that_moment(self):
        ev = [{"t": 100, "ev": "lane.start", "lane": "a-1", "title": "Scan logs", "model": "sol 6.2"},
              {"t": 110, "ev": "lane.start", "lane": "a-2", "title": "Check drive", "model": "haiku"},
              {"t": 150, "ev": "lane.done", "lane": "a-2"},
              {"t": 500, "ev": "lane.done", "lane": "a-1"}]
        with patch.object(L, "lane_summary", lambda k: "Drive is fine." if k == "a-2" else ""):
            out = L.live_at(ev, 210)
        self.assertIn("Lanes running now: 1 (1 sol 6.2)", out)
        self.assertIn("- running: Scan logs (sol 6.2, 110 s)", out)
        self.assertIn("- finished 1 min ago, Check drive: Drive is fine.", out)


class SummarizeTests(unittest.TestCase):
    def test_u3_needs_useful_and_fast_on_answerable_cases(self):
        labels = {"a": "A", "b": "A", "t": "T"}
        rows = [{"case": "a", "arm": "x", "score": 8, "t": 2.0, "text": "yes", "wrong": False},
                {"case": "b", "arm": "x", "score": 9, "t": 3.5, "text": "late", "wrong": False},
                {"case": "t", "arm": "x", "score": 0, "t": 1.0, "text": "Done, it worked.", "wrong": True}]
        s = L.summarize(rows, labels)["x"]
        self.assertEqual(s["U3"], 0.5)        # b was useful but past 3 s
        self.assertEqual(s["useful_A"], 1.0)
        self.assertEqual(s["TF_wrong"], 1)
        self.assertEqual(s["TF_spoke"], 1.0)

    def test_synthetic_cases_parse(self):
        import json
        cases = json.loads((Path(__file__).resolve().parent / "bench/lane-replay-synthetic.json").read_text())
        self.assertTrue(all({"id", "call", "label", "request", "exchanges"} <= set(c) for c in cases))


if __name__ == "__main__":
    unittest.main()
