import json
import tempfile
import unittest
from pathlib import Path

import tiers


class LiveFacts(unittest.TestCase):
    def test_running_lanes_and_newest_results(self):
        st = {"asks": [{"lanes": [
            {"title": "OSS factory status", "state": "running", "model": "terra 6.1", "t0": 990},
            {"title": "Sandbox count", "state": "done", "model": "haiku 5.5", "t1": 940,
             "card": {"summary": "9 boat sandboxes live, all idle."}},
            {"title": "Broken", "state": "failed", "model": "sol 6.2"}]}]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text(json.dumps(st))
            s = tiers.live_facts(now=1000, path=p)
        self.assertIn("Lanes running now: 1 (1 terra 6.1)", s)
        self.assertIn("running: OSS factory status (terra 6.1, 10 s)", s)
        self.assertIn("finished 1 min ago, Sandbox count: 9 boat sandboxes live, all idle.", s)
        self.assertNotIn("Broken", s)

    def test_missing_state_is_empty(self):
        self.assertEqual(tiers.live_facts(path=Path("/nonexistent/state.json")), "")

    def test_snapshot_puts_live_first_and_system_allows_it(self):
        s = tiers.snapshot("chat line", [("user", "hi")], live="Live lanes (from the TV conductor):\nLanes running now: 0")
        self.assertTrue(s.startswith("Live lanes"))
        self.assertIn("chat line", s)
        self.assertIn("Live lanes", tiers.SYSTEM)

    def test_snapshot_without_live_is_unchanged(self):
        self.assertEqual(tiers.snapshot("b", [("user", "hi")]), "Context from his Hermes chat:\nb\n\nThis call so far:\nKevin: hi")


if __name__ == "__main__":
    unittest.main()
