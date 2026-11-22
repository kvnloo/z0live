"""python3 -m unittest test_es4_brief  (from voice-adapter/bench/ghci)

GH12: the even half (GH9) with filler pair 4 (sci-fi books, 313 chars) replaced in place by odd filler pair 1
(weeknight dinner, 333 chars, the closest-length odd pair). Length and every position are fixed, and only the
content of one filler pair changes. This tests whether pair-4 content carries the even half's advantage over the odd half.
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


class Es4Brief(unittest.TestCase):
    def setUp(self):
        self.es4 = make_half.variant_brief(FULL["brief"], "es4")
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_existing_variants_unchanged(self):
        self.assertEqual(make_half.variant_brief(FULL["brief"], 0), self.even)
        self.assertEqual(make_half.variant_brief(FULL["brief"], 1), make_half.half_brief(FULL["brief"], parity=1))
        self.assertEqual(make_half.variant_brief(FULL["brief"], "e5"),
                         make_half.half_brief(FULL["brief"], parity=0, drop=(0,)))
        for name, key in (("slm-v2-half", 0), ("slm-v2-odd", 1), ("slm-v2-e5", "e5")):
            committed = (HERE / f"suites/{name}.json").read_text()
            rebuilt = json.dumps(make_half.half_suite(FULL, key), indent=1, ensure_ascii=False) + "\n"
            self.assertEqual(committed, rebuilt, name)  # byte-identical

    def test_is_even_with_pair4_swapped_for_pair1_in_place(self):
        full_f, even_f, es4_f = filler(FULL["brief"]), filler(self.even), filler(self.es4)
        expect = list(even_f)
        expect[2] = full_f[1]  # even_f[2] is full filler 4
        self.assertEqual(even_f[2], full_f[4])
        self.assertEqual(es4_f, expect)
        e_lines, s_lines = self.even.split("\n"), self.es4.split("\n")
        self.assertEqual(len(e_lines), len(s_lines))
        changed = [i for i, (a, b) in enumerate(zip(e_lines, s_lines)) if a != b]
        self.assertEqual([e_lines[i] for i in changed], list(full_f[4]))
        self.assertEqual([s_lines[i] for i in changed], list(full_f[1]))

    def test_length_delta_is_closest_odd(self):
        full_f = filler(FULL["brief"])
        deltas = {j: plen(full_f[j]) - plen(full_f[4]) for j in range(1, 12, 2)}
        self.assertEqual(min(deltas, key=lambda j: abs(deltas[j])), 1)
        self.assertEqual(deltas[1], 20)
        self.assertEqual(len(self.es4) - len(self.even), 20)
        self.assertNotIn("Project Hail Mary", self.es4)
        self.assertIn("sheet-pan chicken", self.es4)

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-es4.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, "es4"))
        self.assertEqual(suite["suite"], "slm-v2-es4")
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_and_workflow_register_es4(self):
        self.assertIn('"slm-v2-es4": HERE / "suites/slm-v2-es4.json"', (HERE / "ci_bench.py").read_text())
        wf = (HERE.parents[2] / ".github/workflows/text-climb.yml").read_text()
        self.assertIn("es4) SUITES=slm-v2-es4", wf)


if __name__ == "__main__":
    unittest.main()
