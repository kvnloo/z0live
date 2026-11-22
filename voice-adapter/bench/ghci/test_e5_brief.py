"""python3 -m unittest test_e5_brief  (from voice-adapter/bench/ghci)

GH11: the even half (GH9) minus filler pair 0 (pairs 2,4,...,10), to test whether the first filler position
carries the even half's advantage over the odd half (GH10). Only filler pair 0 differs from the even half.
"""
import json
import unittest
from pathlib import Path

import make_half
import make_medium

HERE = Path(__file__).resolve().parent
FULL = json.loads((HERE.parent / "slm-v2.json").read_text())


def filler(brief):
    turns = brief.split("\n")[1:-1]
    return [(k, h) for k, h in zip(turns[0::2], turns[1::2]) if not any(m in h for m in make_medium.FACT_MARKERS)]


class E5Brief(unittest.TestCase):
    def setUp(self):
        self.e5 = make_half.variant_brief(FULL["brief"], "e5")
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_even_and_odd_unchanged(self):
        self.assertEqual(make_half.variant_brief(FULL["brief"], 0), self.even)
        self.assertEqual(make_half.variant_brief(FULL["brief"], 1), make_half.half_brief(FULL["brief"], parity=1))

    def test_is_even_minus_pair0(self):
        full_f, even_f, e5_f = filler(FULL["brief"]), filler(self.even), filler(self.e5)
        self.assertEqual(e5_f, full_f[2::2])
        self.assertEqual(e5_f, even_f[1:])
        removed = [ln for ln in self.even.split("\n") if ln not in self.e5.split("\n")]
        self.assertEqual(removed, list(full_f[0]))

    def test_keeps_header_status_facts_in_order(self):
        full_lines = FULL["brief"].split("\n")
        lines = self.e5.split("\n")
        self.assertEqual((lines[0], lines[-1]), (full_lines[0], full_lines[-1]))
        for ln in make_medium.medium_brief(FULL["brief"]).split("\n"):
            self.assertIn(ln, lines)
        idx = [full_lines.index(ln) for ln in lines]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(len(lines), 24)
        self.assertNotIn("write-ahead log", self.e5)  # filler 0 dropped
        self.assertIn("sourdough", self.e5)           # filler 10 kept

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-e5.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, "e5"))
        self.assertEqual(suite["suite"], "slm-v2-e5")
        self.assertEqual(suite["cases"], FULL["cases"])
        for name, key in (("slm-v2-half", 0), ("slm-v2-odd", 1)):
            self.assertEqual(json.loads((HERE / f"suites/{name}.json").read_text()), make_half.half_suite(FULL, key))

    def test_ci_bench_registers_e5(self):
        self.assertIn('"slm-v2-e5": HERE / "suites/slm-v2-e5.json"', (HERE / "ci_bench.py").read_text())


if __name__ == "__main__":
    unittest.main()
