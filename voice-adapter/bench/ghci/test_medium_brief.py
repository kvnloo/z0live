"""python3 -m unittest test_medium_brief  (from voice-adapter/bench/ghci)"""
import json
import unittest
from pathlib import Path

import make_medium

HERE = Path(__file__).resolve().parent
FULL = json.loads((HERE.parent / "slm-v2.json").read_text())


class MediumBrief(unittest.TestCase):
    def test_keeps_chat_turn_framing(self):
        lines = make_medium.medium_brief(FULL["brief"]).split("\n")
        self.assertEqual(lines[0], "Recent messages in Kevin's Hermes chat (newest last):")
        self.assertTrue(lines[-1].startswith("Status:"))
        turns = lines[1:-1]
        self.assertEqual(len(turns), 12)  # 6 Kevin/Hermes pairs
        for i, ln in enumerate(turns):
            self.assertTrue(ln.startswith("Kevin: " if i % 2 == 0 else "Hermes: "), ln)
        for fact in ("Dr. Patel", "UA 1184", "$1,800", "GPT-Live is the default", "36 GB", "adb screen access"):
            self.assertIn(fact, "\n".join(turns))

    def test_drops_filler_and_length_band(self):
        brief = make_medium.medium_brief(FULL["brief"])
        self.assertNotIn("write-ahead log", brief)
        self.assertNotIn("sourdough", brief)
        self.assertTrue(900 <= len(brief) <= 1050, len(brief))  # ~967 vs full 5545, compressed 579
        self.assertTrue(all(ln in FULL["brief"].split("\n") for ln in brief.split("\n")))  # verbatim lines only

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-chat1k.json").read_text())
        self.assertEqual(suite, make_medium.medium_suite(FULL))
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_registers_medium(self):
        self.assertIn('"slm-v2-chat1k": HERE / "suites/slm-v2-chat1k.json"', (HERE / "ci_bench.py").read_text())


if __name__ == "__main__":
    unittest.main()
