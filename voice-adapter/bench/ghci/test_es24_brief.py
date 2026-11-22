"""python3 -m unittest test_es24_brief  (from voice-adapter/bench/ghci)

GH15: the even half (GH9) with filler pairs 2 (heat pump, 499 chars, the slot right before the "Harsha's flight" fact)
and 4 (sci-fi books, 313 chars, the slot right after it) swapped in position. Same content set, same total length.
Control arm for GH13/GH14: does any even pair in the pre-Harsha slot keep u-wait, or is pair 2 specifically protective?
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


class Es24Brief(unittest.TestCase):
    def setUp(self):
        self.es24 = make_half.variant_brief(FULL["brief"], "es24")
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_existing_variants_rebuild_byte_identical(self):
        for name, key in (("slm-v2-half", 0), ("slm-v2-odd", 1), ("slm-v2-e5", "e5"), ("slm-v2-es4", "es4"),
                          ("slm-v2-es2", "es2"), ("slm-v2-es3", "es3")):
            committed = (HERE / f"suites/{name}.json").read_text()
            rebuilt = json.dumps(make_half.half_suite(FULL, key), indent=1, ensure_ascii=False) + "\n"
            self.assertEqual(committed, rebuilt, name)

    def test_is_even_with_pairs_2_and_4_swapped(self):
        full_f, even_f, es24_f = filler(FULL["brief"]), filler(self.even), filler(self.es24)
        self.assertEqual((even_f[1], even_f[2]), (full_f[2], full_f[4]))
        expect = list(even_f)
        expect[1], expect[2] = full_f[4], full_f[2]
        self.assertEqual(es24_f, expect)
        self.assertEqual(sorted(es24_f), sorted(even_f))

    def test_pre_harsha_slot_holds_pair4(self):
        lines = self.es24.split("\n")
        i = next(n for n, l in enumerate(lines) if l == "Kevin: and Harsha's flight?")
        full_f = filler(FULL["brief"])
        self.assertEqual(tuple(lines[i - 2:i]), full_f[4])
        self.assertEqual(tuple(lines[i + 2:i + 4]), full_f[2])
        e_lines = self.even.split("\n")
        changed = [n for n, (a, b) in enumerate(zip(e_lines, lines)) if a != b]
        self.assertEqual(changed, [i - 2, i - 1, i + 2, i + 3])

    def test_length_unchanged(self):
        self.assertEqual(len(self.es24), len(self.even))
        self.assertEqual(len(self.es24.split("\n")), len(self.even.split("\n")))
        self.assertIn("heat pump", self.es24)
        self.assertIn("Project Hail Mary", self.es24)
        self.assertNotIn("semaphore", self.es24)
        self.assertNotIn("houseplants", self.es24)

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-es24.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, "es24"))
        self.assertEqual(suite["suite"], "slm-v2-es24")
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_and_workflow_register_es24(self):
        self.assertIn('"slm-v2-es24": HERE / "suites/slm-v2-es24.json"', (HERE / "ci_bench.py").read_text())
        wf = (HERE.parents[2] / ".github/workflows/text-climb.yml").read_text()
        self.assertIn("es24) SUITES=slm-v2-es24", wf)


if __name__ == "__main__":
    unittest.main()
