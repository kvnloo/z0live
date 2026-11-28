"""C1-ctx-sem-r5 oracle: prompt / pair-file / summary invariants (no model)."""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "bench"))
import brief_compress_sem2 as sem2  # noqa: E402
import brief_compress_sem5 as sem5  # noqa: E402
import ctx_compress_sem_r5_oracle as orc  # noqa: E402


class PromptTest(unittest.TestCase):
    def test_format_and_parse(self):
        t = sem5.JUDGE_PROMPT_R5.format(old="A-1", new="B-2")
        self.assertTrue(t.rstrip().endswith("Answer with one word."))
        self.assertIn("OLDER: A-1\nNEWER: B-2\nAnswer with one word.", t)
        self.assertIs(sem5.parse_judge, sem2.parse_judge)
        self.assertEqual(sem2.parse_judge("REPLACE"), "replace")
        self.assertEqual(sem2.parse_judge("ALSO"), "also")

    def test_old_prompt_untouched(self):
        self.assertIn("Does the NEWER statement give a replacement", sem2.JUDGE_PROMPT)

    def test_no_few_shot_leak(self):
        d = json.loads(orc.PAIRS.read_text())
        shots = [ln for ln in sem5.JUDGE_PROMPT_R5.splitlines() if ln.startswith(("OLDER: ", "NEWER: ")) and "{" not in ln]
        self.assertEqual(len(shots), 8)
        texts = {p["older"] for p in d["pairs"]} | {p["newer"] for p in d["pairs"]}
        for ln in shots:
            self.assertNotIn(ln.split(": ", 1)[1], texts)


class PairsTest(unittest.TestCase):
    def test_pairs(self):
        d = json.loads(orc.PAIRS.read_text())
        ps = d["pairs"]
        self.assertEqual([p["id"] for p in ps], list(range(len(ps))))
        self.assertEqual(sum(not p["extra"] for p in ps), 188)
        self.assertEqual(sum(p["ambiguous"] for p in ps), 2)
        self.assertEqual(sum(1 for p in ps if p["gold"] == "replace" and p["sem3"] == "also" and not p["extra"]), 2)


class SummaryTest(unittest.TestCase):
    def rows(self, ps, new, old=None):
        r = {}
        for p in ps:
            o = old.get(p["id"], p["sem3"]) if old else p["sem3"]
            r[p["id"]] = {"old1": {"verdict": o}, "old2": {"verdict": o}, "new": {"verdict": new(p)}}
        return r

    def test_pass_and_stop(self):
        ps = json.loads(orc.PAIRS.read_text())["pairs"]
        gold = lambda p: p["gold"] or p["sem3"]  # noqa: E731
        s = orc.summarize(ps, self.rows(ps, gold))
        self.assertEqual(len(s["fixed"]), 2)
        self.assertEqual(s["new_false_replace"], [])
        self.assertTrue(s["proceed_to_scored_run"])
        s2 = orc.summarize(ps, self.rows(ps, lambda p: "replace"))
        self.assertGreater(len(s2["new_false_replace"]), 0)
        self.assertEqual(s2["outcome"], "NULL(oracle-stop)")
        s3 = orc.summarize(ps, self.rows(ps, gold, {0: "replace" if ps[0]["sem3"] == "also" else "also"}))
        self.assertGreater(s3["aa_flips_total"], 0)
        self.assertFalse(s3["proceed_to_scored_run"])


if __name__ == "__main__":
    unittest.main()
