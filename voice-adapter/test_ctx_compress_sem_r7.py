"""C1-ctx-sem-r7 oracle: summary / verdict invariants on fake rows (no model)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "bench"))
import ctx_compress_sem_r7_oracle as o  # noqa: E402
import ctx_compress_sem_r7_verdict as v  # noqa: E402


class R7Test(unittest.TestCase):
    def setUp(self):
        self.pairs = json.loads(o.PAIRS.read_text())["pairs"]
        self.base = {int(k): x for k, x in json.loads(o.BASE.read_text())["verdicts"].items()}

    def rows(self, f):
        return {p["id"]: {a: {"reply": f(p), "verdict": f(p), "wall_s": 1.0} for a in ("p1", "p2")} for p in self.pairs}

    def test_base_covers_all_pairs_and_matches_frozen_counts(self):
        self.assertEqual(len(self.pairs), 198)
        self.assertEqual(set(self.base), {p["id"] for p in self.pairs})
        ups = [p for p in self.pairs if p["gold"] == "replace" and not p["ambiguous"]]
        self.assertEqual(sum(self.base[p["id"]] == "replace" for p in ups), 63)
        self.assertEqual(sum(self.base[p["id"]] == "replace" for p in self.pairs if p["class"] == "noise"), 1)

    def test_oracle_agrees_with_independent_verdict(self):
        for f in (lambda p: self.base[p["id"]], lambda p: "replace" if p["gold"] == "replace" else "also", lambda p: "replace"):
            rows = self.rows(f)
            s = o.summarize(self.pairs, rows, self.base)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump({"rows": rows}, fh)
            w = v.verdict(fh.name)
            self.assertEqual(s["outcome"], w["outcome"])
            self.assertEqual(s["fixed"], w["fixed"])
            self.assertEqual(s["new_false_replace_blind"], w["new_false_replace_blind"])

    def test_e2b_replay_is_null_and_oracle_gold_without_noise_passes(self):
        self.assertEqual(o.summarize(self.pairs, self.rows(lambda p: self.base[p["id"]]), self.base)["outcome"], "NULL(oracle-stop)")
        s = o.summarize(self.pairs, self.rows(lambda p: "replace" if p["gold"] == "replace" else "also"), self.base)
        self.assertEqual(s["outcome"], "PASS(oracle)")
        self.assertEqual(len(s["fixed"]), 2)

    def test_all_replace_fails_new_false_replace_and_noise(self):
        s = o.summarize(self.pairs, self.rows(lambda p: "replace"), self.base)
        self.assertEqual(s["outcome"], "NULL(oracle-stop)")
        self.assertGreater(len(s["noise_replace_e4b"]), 1)

    def test_aa_flip_fails(self):
        rows = self.rows(lambda p: "replace" if p["gold"] == "replace" else "also")
        rows[self.pairs[0]["id"]]["p2"]["verdict"] = "replace" if rows[self.pairs[0]["id"]]["p1"]["verdict"] == "also" else "also"
        self.assertEqual(o.summarize(self.pairs, rows, self.base)["outcome"], "NULL(oracle-stop)")

    def test_percentile(self):
        self.assertEqual(o.pct([1, 2, 3, 4, 5], 0.5), 3)


if __name__ == "__main__":
    unittest.main()
