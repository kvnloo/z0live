"""C1-ctx-compress driver + verdict tests (no model)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))
import context_eval  # noqa: E402
import ctx_compress_eval as cce  # noqa: E402
import ctx_compress_verdict as ccv  # noqa: E402
import tiers  # noqa: E402


class Arms(unittest.TestCase):
    def test_arm_a_is_context_eval_snapshot_unchanged(self):
        for s in context_eval.suite():
            self.assertEqual(cce.context(s, "A"), context_eval.snapshot(s))
            self.assertEqual(cce.system(s, "A"), tiers.SYSTEM.format(tier="fast") + "\n\n" + context_eval.snapshot(s))

    def test_chat_brief_is_the_brief_context_eval_uses(self):
        for s in context_eval.suite():
            self.assertEqual(tiers.snapshot(cce.chat_brief(s), context_eval.exchange(s.call)), context_eval.snapshot(s))

    def test_only_the_brief_differs(self):
        for s in context_eval.suite():
            b = cce.context(s, "B")
            self.assertLessEqual(len(b.split("\n\nThis call so far:\n")[0]), len("Context from his Hermes chat:\n") + 2810)
            self.assertEqual(b.split("\n\nThis call so far:\n")[1], cce.context(s, "A").split("\n\nThis call so far:\n")[1])
            with mock.patch.object(cce, "BUDGET", 10 ** 6):
                self.assertEqual(cce.context(s, "B"), cce.context(s, "A"))

    def test_run_block_uses_context_eval_score(self):
        s = context_eval.suite()[0]

        def fake(system, user):
            p = next(p for p in s.probes if p.ask == user)
            return (p.current or "SKIP"), 0.1, 0.2, 0.3, {"prompt_n": 5}
        with mock.patch.object(cce.lanespeed, "ask", side_effect=fake):
            rows, stopped = cce.run_block([s], "B", cce.time.monotonic(), 999)
        self.assertFalse(stopped)
        self.assertEqual(len(rows), 8)
        self.assertEqual(cce.recall_n(rows), 6)
        self.assertEqual([r["result"] for r in rows if r["current"] is None], ["SKIP", "SKIP"])


def job(name, recall):
    blocks = []
    for lab, hits in recall.items():
        rows = [{"scenario": name, "key": f"k{i}", "current": "x", "result": "RECALL" if i in hits else "SKIP",
                 "first_sentence_s": 1.0 if lab[0] == "A" else 0.5} for i in range(6)]
        blocks.append({"label": lab, "rows": rows})
    return {"scenarios": [name], "complete": True, "blocks": blocks}


class Verdict(unittest.TestCase):
    def jobs(self, b2):
        same = {"A1": {0, 1, 2}, "B1": {0, 1, 2}, "A2": {0, 1, 2}, "B2": b2}
        return [job(f"workshop-{i}", same) for i in (1, 2)] + [job("workshop-3", {**same, "A2": {0, 1}})]

    def test_keep_within_band(self):
        v = ccv.verdict(self.jobs({0, 1, 2}))
        self.assertEqual((v["outcome"], v["band"], v["recall"]["A1"], v["recall"]["A2"]), ("KEEP", 1, 9, 8))
        self.assertEqual(v["first_sentence_p50_s"], {"A": 1.0, "B": 0.5})

    def test_keep_at_band_edge_and_revert_below(self):
        jobs = self.jobs({0, 1, 2})
        jobs[0]["blocks"][3]["rows"][2]["result"] = "SKIP"                   # B2 = 8 >= max(A) 9 - band 1
        self.assertEqual(ccv.verdict(jobs)["outcome"], "KEEP")
        self.assertEqual(ccv.verdict(self.jobs({0, 1}))["outcome"], "REVERT")  # B2 = 6 < 9 - 1
        v = ccv.verdict([job(f"workshop-{i}", {"A1": {0, 1, 2}, "B1": {0, 1, 2}, "A2": {0, 1, 2}, "B2": {0}})
                         for i in (1, 2, 3)])
        self.assertEqual((v["outcome"], v["band"], v["bb_flips"]), ("REVERT", 0, 6))  # B/B flips never widen the band
        v = ccv.verdict([job(f"workshop-{i}", {"A1": {0, 1, 2}, "B1": {0}, "A2": {0, 1, 2}, "B2": {0}})
                         for i in (1, 2, 3)])
        self.assertEqual((v["outcome"], v["band"]), ("REVERT", 0))

    def test_null_on_incomplete_error_or_missing(self):
        jobs = self.jobs({0, 1, 2})
        jobs[0]["complete"] = False
        self.assertEqual(ccv.verdict(jobs)["outcome"], "NULL")
        jobs = self.jobs({0, 1, 2})
        jobs[1]["blocks"][0]["rows"][0]["result"] = "ERROR"
        self.assertEqual(ccv.verdict(jobs)["outcome"], "NULL")
        self.assertEqual(ccv.verdict(self.jobs({0, 1, 2})[:2])["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()
