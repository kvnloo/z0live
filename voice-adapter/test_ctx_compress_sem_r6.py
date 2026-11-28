"""C1-ctx-sem-r6 oracle: margin / threshold / summary invariants (no model)."""
import json
import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "bench"))
import ctx_compress_sem_r6_margin as m  # noqa: E402


class MarginTest(unittest.TestCase):
    def test_margin_sums_variants(self):
        top = [{"token": "RE", "logprob": math.log(0.5)}, {"token": " RE", "logprob": math.log(0.1)},
               {"token": "AL", "logprob": math.log(0.2)}, {"token": "X", "logprob": math.log(0.1)}]
        mg, pr, pa = m.margin_of(top, {"RE", " RE"}, {"AL"})
        self.assertAlmostEqual(pr, 0.6)
        self.assertAlmostEqual(pa, 0.2)
        self.assertAlmostEqual(mg, math.log(3.0))

    def test_missing_group_is_floored(self):
        mg, pr, pa = m.margin_of([{"token": "AL", "logprob": -0.1}], {"RE"}, {"AL"})
        self.assertEqual(pr, 0.0)
        self.assertLess(mg, -20)

    def test_fit_threshold(self):
        f = m.fit_threshold([(-5.0, "neg"), (-2.0, "neg"), (-3.0, "pos"), (1.0, "pos"), (3.0, "pos")])
        self.assertAlmostEqual(f["t"], -0.5)       # m_neg=-2, lowest pos above = 1
        self.assertEqual(f["dev_pos_lost"], 1)
        f = m.fit_threshold([(2.0, "neg"), (1.0, "pos")])
        self.assertAlmostEqual(f["t"], 3.0)

    def test_auc(self):
        self.assertEqual(m.auc([2, 3], [0, 1]), 1.0)
        self.assertEqual(m.auc([1], [1]), 0.5)

    def test_summary_runs_on_fake_rows(self):
        pairs = json.loads(m.PAIRS.read_text())["pairs"]
        self.assertEqual({p["set"] for p in pairs if p["set"] in m.DEV_SETS + m.BLIND_SETS}, set(m.DEV_SETS + m.BLIND_SETS))
        self.assertEqual({p["set"] for p in pairs}, set(m.DEV_SETS + m.BLIND_SETS))
        rows = {}
        for p in pairs:
            g = 4.0 if p["gold"] == "replace" else -4.0
            r = {"reply": "x", "verdict": "replace" if g > 0 else "also", "margin": g, "p_replace": 0, "p_also": 0,
                 "first": "t", "top1": "t", "n_top": 1}
            rows[p["id"]] = {"p1": dict(r), "p2": dict(r)}
        s = m.summarize(pairs, rows)
        self.assertEqual(s["aa_decision_flips"], [])
        self.assertEqual(s["auc_update_vs_still_true"], 1.0)
        self.assertIn(s["outcome"], ("PASS(oracle)", "NULL(oracle-stop)"))


if __name__ == "__main__":
    unittest.main()
