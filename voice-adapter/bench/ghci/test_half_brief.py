"""python3 -m unittest test_half_brief  (from voice-adapter/bench/ghci)"""
import json
import unittest
from pathlib import Path

import make_half
import make_medium

HERE = Path(__file__).resolve().parent
FULL = json.loads((HERE.parent / "slm-v2.json").read_text())
FACTS = ("Dr. Patel", "UA 1184", "$1,800", "GPT-Live is the default", "36 GB", "adb screen access")


class HalfBrief(unittest.TestCase):
    def setUp(self):
        self.brief = make_half.half_brief(FULL["brief"])
        self.lines = self.brief.split("\n")

    def test_keeps_header_status_and_fact_turns(self):
        full_lines = FULL["brief"].split("\n")
        self.assertEqual(self.lines[0], full_lines[0])
        self.assertEqual(self.lines[-1], full_lines[-1])
        self.assertTrue(self.lines[-1].startswith("Status:"))
        for ln in make_medium.medium_brief(FULL["brief"]).split("\n"):
            self.assertIn(ln, self.lines)  # every medium (fact) line kept verbatim
        for fact in FACTS:
            self.assertIn(fact, self.brief)

    def test_filler_pair_count_and_order(self):
        turns = self.lines[1:-1]
        self.assertEqual(len(turns), 24)  # 6 fact + 6 filler pairs
        for i, ln in enumerate(turns):
            self.assertTrue(ln.startswith("Kevin: " if i % 2 == 0 else "Hermes: "), ln)
        filler = [h for h in turns[1::2] if not any(m in h for m in make_medium.FACT_MARKERS)]
        self.assertEqual(len(filler), 6)
        full_lines = FULL["brief"].split("\n")
        idx = [full_lines.index(ln) for ln in self.lines]
        self.assertEqual(idx, sorted(idx))  # original order, verbatim lines only
        self.assertIn("write-ahead log", self.brief)  # filler 0 kept
        self.assertNotIn("sheet-pan", self.brief)     # filler 1 dropped
        self.assertIn("sourdough", self.brief)        # filler 10 kept
        self.assertNotIn("hiragana", self.brief)      # filler 11 dropped

    def test_length_band(self):
        self.assertTrue(3000 <= len(self.brief) <= 3500, len(self.brief))  # ~3.2k vs full 5545, medium 967

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-half.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL))
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_registers_half(self):
        self.assertIn('"slm-v2-half": HERE / "suites/slm-v2-half.json"', (HERE / "ci_bench.py").read_text())


if __name__ == "__main__":
    unittest.main()
