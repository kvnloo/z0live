"""C1-ctx-compress-r9 harness: arms, sets, the frozen offline dry prediction, and the frozen r9 verdict."""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r9_eval as r9  # noqa: E402
import ctx_compress_r9_verdict as v9  # noqa: E402

FIXTURE = HERE / "fixtures" / "ctx_compress_r4_jobs.json"
# Offline prediction (frozen in the PREREG and dry-r9.json): what each arm's context holds, per probe, identical in
# all 3 scenarios of each set.
REC = {"A": {"bench": "STALE", "badge": "STALE", "glaze": "CURRENT"},
       "B": {"bench": "CURRENT", "badge": "CURRENT", "glaze": "CURRENT"},
       "R": {"bench": "CURRENT", "badge": "CURRENT", "glaze": "CURRENT"},
       "O": {"bench": "BOTH", "badge": "BOTH", "glaze": "CURRENT"}}
R8 = {"A": {"a1": "STALE", "a2": "STALE", "d": "CURRENT", "b1": "STALE", "b2": "STALE",
            "c_old": "CURRENT", "c_new": "ABSENT"},
      "B": {"a1": "CURRENT", "a2": "CURRENT", "d": "CURRENT", "b1": "STALE", "b2": "STALE",
            "c_old": "CURRENT", "c_new": "ABSENT"},
      "R": {"a1": "CURRENT", "a2": "CURRENT", "d": "CURRENT", "b1": "STALE", "b2": "STALE",
            "c_old": "ABSENT", "c_new": "CURRENT"},
      "O": {"a1": "BOTH", "a2": "BOTH", "d": "CURRENT", "b1": "BOTH", "b2": "BOTH",
            "c_old": "CURRENT", "c_new": "CURRENT"}}
SUP = ("bench", "badge", "a1", "a2", "b1", "b2")


class Harness(unittest.TestCase):
    def test_arms(self):
        self.assertEqual(sorted(r9.COMPRESS), ["A", "B", "O", "R"])
        self.assertEqual(r9.BUDGET, 2810)

    def test_install(self):
        saved = (r2.COMPRESS, r2.dry, r2.suite, context_eval.score)
        try:
            for which, first, score in (("main", "workshop-1", r9.SCORE), ("recency", "recency-1", r6.score_recency),
                                        ("r8", "r8-allotment", r6.score_recency)):
                r9.install(which)
                self.assertEqual(r2.suite()[0].name, first)
                self.assertIs(context_eval.score, score)
                self.assertIs(r2.COMPRESS, r9.COMPRESS)
        finally:
            r2.COMPRESS, r2.dry, r2.suite, context_eval.score = saved

    def test_dry_prediction(self):
        for suite, want in ((r6.recency_suite(), REC), (r8.r8_suite(), R8)):
            for row in r9.dry(suite):
                for arm in "ABRO":
                    self.assertEqual(row[arm]["items"], want[arm], (row["scenario"], arm))
                for arm in "ABR":
                    self.assertLessEqual(row[arm]["brief_chars"], r9.BUDGET)

    def test_main_set_briefs_identical(self):
        for row in r9.dry(r9.R2_SUITE()):
            self.assertEqual(row["A"], row["B"])
            self.assertEqual(row["A"], row["R"])


def main_jobs():
    jobs = json.loads(FIXTURE.read_text())["jobs"]
    for j in jobs:
        j["r9_set"] = "main"
    return jobs


def predicted(lab, key):
    """Gemma answers from the context: CURRENT -> CORRECT/RECALL, STALE -> STALE, ABSENT -> ABSTAIN."""
    want = REC[lab[0]].get(key) or R8[lab[0]][key]
    if want == "STALE":
        return "STALE"
    if want == "ABSENT":
        return "ABSTAIN"
    return "CORRECT" if key in SUP else "RECALL"


def set_jobs(which, results=None, base=predicted):
    results = results or {}
    suite, halves = ((r6.recency_suite(), ((0, 1, 2),)) if which == "recency"
                     else (r8.r8_suite(), ((0, 1, 2), (3, 4, 5, 6))))
    jobs = []
    for s in suite:
        for idx in halves:
            blocks = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key), base(lab, s.probes[k].key))} for k in idx]}
                for lab in v9.LABELS]
            jobs.append({"r9_set": which, "scenarios": [s.name], "complete": True, "blocks": blocks})
    return jobs


def all_jobs(results=None):
    return main_jobs() + set_jobs("recency", results) + set_jobs("r8", results)


def every(lab, key, res, names=r8.R8_NAMES):
    return {(lab, n, key): res for n in names}


class Verdict(unittest.TestCase):
    def test_dry_prediction_is_keep(self):
        v = v9.verdict(all_jobs())
        self.assertEqual(v["outcome"], "KEEP", (v["null_reasons"], v["failed"]))
        self.assertEqual(v["fm_loss"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 3})
        self.assertEqual(v["recency_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0, "R1": 0})
        self.assertEqual(v["r8a_stale"]["B1"], 0)
        self.assertEqual(v["band"], 0)

    def test_false_merge_reverts_on_g1(self):
        res = {**every("B1", "c_old", "INVENTED"), **every("B2", "c_old", "INVENTED")}  # as r7 did in r8
        v = v9.verdict(all_jobs(res))
        self.assertEqual(v["outcome"], "REVERT")
        self.assertIn("G1_fm_loss", v["failed"])
        self.assertIn("G7_no_new_invented", v["failed"])

    def test_stale_not_fixed_reverts(self):
        res = {**every("B1", "bench", "STALE", r6.RECENCY_NAMES), **every("B2", "bench", "STALE", r6.RECENCY_NAMES),
               **every("B1", "badge", "STALE", r6.RECENCY_NAMES), **every("B2", "badge", "STALE", r6.RECENCY_NAMES)}
        v = v9.verdict(all_jobs(res))
        self.assertEqual(v["failed"], ["G2_recency_stale_fixed"])
        res = {**every("B1", "a1", "STALE"), **every("B2", "a1", "STALE"),
               **every("B1", "a2", "STALE"), **every("B2", "a2", "STALE")}
        self.assertEqual(v9.verdict(all_jobs(res))["failed"], ["G3_r8a_stale_fixed"])

    def test_unique_loss_reverts(self):
        res = {**every("B1", "glaze", "ABSTAIN", r6.RECENCY_NAMES), **every("B2", "glaze", "ABSTAIN", r6.RECENCY_NAMES)}
        self.assertEqual(v9.verdict(all_jobs(res))["failed"], ["G4_unique_kept"])
        # discordant B blocks widen the band (B/B flips 3): one-block loss is absorbed, as in r8's band rule
        v = v9.verdict(all_jobs(every("B1", "d", "ABSTAIN")))
        self.assertEqual((v["band"], v["outcome"]), (3, "KEEP"))

    def test_band_pools_both_sets(self):
        res = {("A2", "recency-2", "bench"): "CORRECT", ("B2", "r8-choir", "c_old"): "ABSTAIN",
               ("B1", "r8-choir", "b1"): "CORRECT"}
        v = v9.verdict(all_jobs(res))
        self.assertEqual((v["aa_flips"], v["bb_flips"], v["band"]), (1, 2, 2))
        self.assertEqual(v["fm_loss"]["B2"], 1)
        self.assertTrue(v["checks"]["G1_fm_loss"])              # max B 1 <= max A 0 + band 2
        self.assertEqual(v["recency_stale"]["A2"], 5)
        self.assertTrue(v["checks"]["G2_recency_stale_fixed"])  # max B 0 < min A 5 - band 2
        self.assertEqual(v["outcome"], "KEEP")

    def test_one_loss_in_both_b_blocks_fails_g1(self):
        # same item lost in B1 and B2: no B/B flip, band 0, so a single LOSS exceeds max A 0 + 0
        res = {("B1", "r8-choir", "c_old"): "ABSTAIN", ("B2", "r8-choir", "c_old"): "ABSTAIN"}
        v = v9.verdict(all_jobs(res))
        self.assertEqual((v["band"], v["fm_loss"]["B1"]), (0, 1))
        self.assertEqual(v["failed"], ["G1_fm_loss"])

    def test_rival_report_only(self):
        res = every("R1", "a1", "ERROR")
        v = v9.verdict(all_jobs(res))
        self.assertEqual(v["outcome"], "KEEP")  # R1 never changes the outcome
        self.assertEqual(v["per_arm"]["R1"]["R8_SAME"]["ERROR"], 3)

    def test_null(self):
        self.assertEqual(v9.verdict(main_jobs() + set_jobs("recency") + set_jobs("r8")[:5])["outcome"], "NULL")
        self.assertEqual(v9.verdict(main_jobs() + set_jobs("r8"))["outcome"], "NULL")
        self.assertEqual(v9.verdict(all_jobs({("A2", "recency-3", "glaze"): "ERROR"}))["outcome"], "NULL")
        self.assertEqual(v9.verdict(main_jobs()[:5] + set_jobs("recency") + set_jobs("r8"))["outcome"], "NULL")
        jobs = all_jobs()
        jobs[-1]["complete"] = False
        self.assertEqual(v9.verdict(jobs)["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()
