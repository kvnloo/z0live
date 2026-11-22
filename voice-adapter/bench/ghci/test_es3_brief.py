"""python3 -m unittest test_es3_brief  (from voice-adapter/bench/ghci)

GH14: the even half (GH9) with filler pair 2 (heat pump, 499 chars, the slot right before the "Harsha's flight" fact)
replaced in place by odd filler pair 3 (houseplants, 352 chars, -147). Positions are fixed and only the content of one
filler pair changes. Tests whether GH13's u-wait loss (pair 5 in that slot) is a slot effect or pair-5 content.
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


def plen(pair):
    return len(pair[0]) + len(pair[1]) + 2


class Es3Brief(unittest.TestCase):
    def setUp(self):
        self.es3 = make_half.variant_brief(FULL["brief"], "es3")
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_existing_variants_rebuild_byte_identical(self):
        for name, key in (("slm-v2-half", 0), ("slm-v2-odd", 1), ("slm-v2-e5", "e5"), ("slm-v2-es4", "es4"), ("slm-v2-es2", "es2")):
            committed = (HERE / f"suites/{name}.json").read_text()
            rebuilt = json.dumps(make_half.half_suite(FULL, key), indent=1, ensure_ascii=False) + "\n"
            self.assertEqual(committed, rebuilt, name)

    def test_is_even_with_pair2_swapped_for_pair3_in_place(self):
        full_f, even_f, es3_f = filler(FULL["brief"]), filler(self.even), filler(self.es3)
        self.assertEqual(even_f[1], full_f[2])  # even_f[1] is full filler 2
        expect = list(even_f)
        expect[1] = full_f[3]
        self.assertEqual(es3_f, expect)
        e_lines, s_lines = self.even.split("\n"), self.es3.split("\n")
        self.assertEqual(len(e_lines), len(s_lines))
        changed = [i for i, (a, b) in enumerate(zip(e_lines, s_lines)) if a != b]
        self.assertEqual([e_lines[i] for i in changed], list(full_f[2]))
        self.assertEqual([s_lines[i] for i in changed], list(full_f[3]))

    def test_length_delta(self):
        full_f = filler(FULL["brief"])
        self.assertEqual(plen(full_f[3]) - plen(full_f[2]), -147)
        self.assertEqual(len(self.es3) - len(self.even), -147)
        self.assertNotIn("heat pump", self.es3)
        self.assertIn("houseplants", self.es3)
        self.assertNotIn("semaphore", self.es3)

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-es3.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, "es3"))
        self.assertEqual(suite["suite"], "slm-v2-es3")
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_and_workflow_register_es3(self):
        self.assertIn('"slm-v2-es3": HERE / "suites/slm-v2-es3.json"', (HERE / "ci_bench.py").read_text())
        wf = (HERE.parents[2] / ".github/workflows/text-climb.yml").read_text()
        self.assertIn("es3) SUITES=slm-v2-es3", wf)


if __name__ == "__main__":
    unittest.main()
