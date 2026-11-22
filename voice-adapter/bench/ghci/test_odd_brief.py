"""python3 -m unittest test_odd_brief  (from voice-adapter/bench/ghci)

GH10: the odd-indexed half (filler pairs 1,3,...,11) is the complement of the even half (GH9) at the same
pair count, so length is held near-equal and pair content is the only change.
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


class OddBrief(unittest.TestCase):
    def setUp(self):
        self.odd = make_half.half_brief(FULL["brief"], parity=1)
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_default_parity_is_even_gh9(self):
        self.assertEqual(make_half.half_brief(FULL["brief"]), self.even)

    def test_odd_is_complement_of_even(self):
        full_f, odd_f, even_f = filler(FULL["brief"]), filler(self.odd), filler(self.even)
        self.assertEqual(len(full_f), 12)
        self.assertEqual(odd_f, full_f[1::2])
        self.assertEqual(even_f, full_f[0::2])
        self.assertFalse(set(odd_f) & set(even_f))

    def test_keeps_header_status_facts_in_order(self):
        full_lines = FULL["brief"].split("\n")
        lines = self.odd.split("\n")
        self.assertEqual((lines[0], lines[-1]), (full_lines[0], full_lines[-1]))
        for ln in make_medium.medium_brief(FULL["brief"]).split("\n"):
            self.assertIn(ln, lines)
        idx = [full_lines.index(ln) for ln in lines]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(len(lines), 26)
        self.assertIn("sheet-pan", self.odd)        # filler 1 kept
        self.assertIn("hiragana", self.odd)         # filler 11 kept
        self.assertNotIn("write-ahead log", self.odd)  # filler 0 dropped
        self.assertNotIn("sourdough", self.odd)     # filler 10 dropped

    def test_length_near_even_half(self):
        self.assertTrue(3000 <= len(self.odd) <= 3600, len(self.odd))
        self.assertLess(abs(len(self.odd) - len(self.even)), 400)

    def test_committed_odd_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-odd.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, parity=1))
        self.assertEqual(suite["suite"], "slm-v2-odd")
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_registers_odd(self):
        self.assertIn('"slm-v2-odd": HERE / "suites/slm-v2-odd.json"', (HERE / "ci_bench.py").read_text())


if __name__ == "__main__":
    unittest.main()
