"""python3 -m unittest test_es2_brief  (from voice-adapter/bench/ghci)

GH13: the even half (GH9) with filler pair 2 (heat pump, 499 chars, the slot right before the "Harsha's flight" fact)
replaced in place by odd filler pair 5 (mutex vs semaphore, 475 chars, the closest-length odd pair). Length and every
position are fixed, and only the content of one filler pair changes.
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


class Es2Brief(unittest.TestCase):
    def setUp(self):
        self.es2 = make_half.variant_brief(FULL["brief"], "es2")
        self.even = make_half.half_brief(FULL["brief"], parity=0)

    def test_existing_variants_rebuild_byte_identical(self):
        for name, key in (("slm-v2-half", 0), ("slm-v2-odd", 1), ("slm-v2-e5", "e5"), ("slm-v2-es4", "es4")):
            committed = (HERE / f"suites/{name}.json").read_text()
            rebuilt = json.dumps(make_half.half_suite(FULL, key), indent=1, ensure_ascii=False) + "\n"
            self.assertEqual(committed, rebuilt, name)

    def test_is_even_with_pair2_swapped_for_pair5_in_place(self):
        full_f, even_f, es2_f = filler(FULL["brief"]), filler(self.even), filler(self.es2)
        self.assertEqual(even_f[1], full_f[2])  # even_f[1] is full filler 2
        expect = list(even_f)
        expect[1] = full_f[5]
        self.assertEqual(es2_f, expect)
        e_lines, s_lines = self.even.split("\n"), self.es2.split("\n")
        self.assertEqual(len(e_lines), len(s_lines))
        changed = [i for i, (a, b) in enumerate(zip(e_lines, s_lines)) if a != b]
        self.assertEqual([e_lines[i] for i in changed], list(full_f[2]))
        self.assertEqual([s_lines[i] for i in changed], list(full_f[5]))

    def test_length_delta_is_closest_odd(self):
        full_f = filler(FULL["brief"])
        deltas = {j: plen(full_f[j]) - plen(full_f[2]) for j in range(1, 12, 2)}
        self.assertEqual(min(deltas, key=lambda j: abs(deltas[j])), 5)
        self.assertEqual(deltas[5], -24)
        self.assertEqual(len(self.es2) - len(self.even), -24)
        self.assertNotIn("heat pump", self.es2)
        self.assertIn("semaphore", self.es2)

    def test_committed_suite_matches_builder_and_cases(self):
        suite = json.loads((HERE / "suites/slm-v2-es2.json").read_text())
        self.assertEqual(suite, make_half.half_suite(FULL, "es2"))
        self.assertEqual(suite["suite"], "slm-v2-es2")
        self.assertEqual(suite["cases"], FULL["cases"])

    def test_ci_bench_and_workflow_register_es2(self):
        self.assertIn('"slm-v2-es2": HERE / "suites/slm-v2-es2.json"', (HERE / "ci_bench.py").read_text())
        wf = (HERE.parents[2] / ".github/workflows/text-climb.yml").read_text()
        self.assertIn("es2) SUITES=slm-v2-es2", wf)


if __name__ == "__main__":
    unittest.main()
