"""C1-ctx-compress-r6 harness: recency suite, recency scorer, frozen r6 verdict (on the r4 fixture + synthetic recency)."""
import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r6_verdict as v6  # noqa: E402

FIXTURE = HERE / "fixtures" / "ctx_compress_r4_jobs.json"


def main_jobs():
    jobs = json.loads(FIXTURE.read_text())["jobs"]
    for j in jobs:
        j["r6_set"] = "main"
    return jobs


def rec_jobs(results=None):
    """3 recency jobs, blocks A1 B1 A2 B2 O1; default: every supersede probe CORRECT, glaze RECALL."""
    results = results or {}
    jobs = []
    for s in r6.recency_suite():
        blocks = []
        for lab in v6.LABELS:
            rows = []
            for p in s.probes:
                res = results.get((lab, s.name, p.key), "CORRECT" if p.superseded else "RECALL")
                rows.append({"scenario": s.name, "key": p.key, "current": p.current,
                             "superseded": list(p.superseded), "reply": "x", "result": res, "first_sentence_s": 1.0})
            blocks.append({"label": lab, "arm": lab[0], "rows": rows})
        jobs.append({"r6_set": "recency", "scenarios": [s.name], "complete": True, "blocks": blocks})
    return jobs


def every(lab, key, res):
    return {(lab, n, key): res for n in v6.RECENCY}


class RecencySuite(unittest.TestCase):
    def test_shape_and_sizes(self):
        suite = r6.recency_suite()
        self.assertEqual([s.name for s in suite], v6.RECENCY)
        for s, base in zip(suite, r2.suite()):
            self.assertEqual(s.stable, base.stable)
            self.assertEqual(s.call, base.call)
            b = r2.chat_brief(s)
            self.assertLess(len(b), 6000)  # full brief (arm O) under tiers.snapshot's cap
            self.assertGreater(len(b), r6.BUDGET)
            ex = brief_compress._split(b.split("\n"))[1]
            sizes = {e[0]: brief_compress._size([e]) for e in ex}
            for e in ex:
                self.assertLessEqual(len(" ".join(e[1:])), brief_compress.FACT_REPLY_CHARS)  # every exchange a fact
            noise = [v for k, v in sizes.items() if "Workshop note" in k]
            pair = {p.key: p for p in s.probes}
            olds = [v for k, v in sizes.items() if any(x in k for x in ("Fern-", "Teal-", "Opal-"))]
            news = [v for k, v in sizes.items() if any(x in k for x in ("Moss-", "Plum-"))]
            self.assertEqual((len(olds), len(news)), (3, 2))
            self.assertLess(max(olds), min(noise))
            self.assertGreater(min(news), max(noise))
            self.assertLessEqual(max(news), min(olds) * (1 + 0.10))  # r6 FACT_BAND (r7: constant gone from production)
            self.assertEqual(sorted(pair), ["badge", "bench", "glaze"])
            # newest two chat exchanges are the NEW values
            self.assertIn(pair["bench"].current, ex[-2][0])
            self.assertIn(pair["badge"].current, ex[-1][0])

    def test_scorer(self):
        p = context_eval.Probe("bench", "q", "Moss-48", ("Fern-21",))
        u = context_eval.Probe("glaze", "q", "Opal-82")
        cases = [(p, "Moss-48", "CORRECT"), (p, "Your workshop bench number is Moss-48.", "CORRECT"),
                 (p, "Fern-21.", "STALE"), (p, "It moved from Fern-21 to Moss-48.", "BOTH"),
                 (p, "SKIP", "ABSTAIN"), (p, "I do not have your bench number.", "ABSTAIN"),
                 (p, "Oak-12", "INVENTED"), (p, "The bench by the window.", "OFF_FORMAT"), (p, "", "ERROR"),
                 (p, "Moss-480", "INVENTED"), (u, "Opal-82", "RECALL"), (u, "I don't know.", "ABSTAIN"),
                 (u, "Cedar-14", "INVENTED")]
        for probe, reply, want in cases:
            self.assertEqual(r6.score_recency(probe, reply), want, reply)

    def test_install_arms(self):
        saved = (r2.COMPRESS, r2.dry, r2.suite, context_eval.score)
        try:
            r6.install("recency")
            s = r2.suite()[0]
            self.assertEqual(s.name, "recency-1")
            b = r2.chat_brief(s)
            self.assertEqual(r2.COMPRESS["O"](b), b)
            self.assertEqual(r2.COMPRESS["B"](b), brief_compress.compress_brief(b, 2810))
            self.assertIs(context_eval.score, r6.score_recency)
        finally:
            r2.COMPRESS, r2.dry, r2.suite, context_eval.score = saved


class Verdict(unittest.TestCase):
    def test_all_correct_is_no_update(self):
        v = v6.verdict(main_jobs() + rec_jobs())
        self.assertEqual(v["outcome"], "NO_UPDATE", v["failed"] + v["null_reasons"])
        self.assertEqual(v["main_r5_gates"]["outcome"], "KEEP")

    def test_b_fixes_stale_is_keep(self):
        res = {**every("A1", "bench", "STALE"), **every("A2", "bench", "STALE")}
        v = v6.verdict(main_jobs() + rec_jobs(res))
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["stale"], {"A1": 3, "B1": 0, "A2": 3, "B2": 0, "O1": 0})

    def test_b_more_stale_is_revert(self):
        v = v6.verdict(main_jobs() + rec_jobs(every("B1", "bench", "STALE") | every("B2", "bench", "STALE")))
        self.assertEqual(v["outcome"], "REVERT")

    def test_band_absorbs_stale_difference(self):
        res = {**every("A1", "bench", "STALE"), ("A2", "recency-1", "bench"): "STALE"}
        v = v6.verdict(main_jobs() + rec_jobs(res))
        self.assertEqual(v["recency"]["band"], 2)
        self.assertEqual(v["outcome"], "NO_UPDATE")  # max B 0 < min A 1 - 2 is false

    def test_unique_loss_reverts_even_with_stale_fix(self):
        res = {**every("A1", "bench", "STALE"), **every("A2", "bench", "STALE"),
               **every("B1", "glaze", "ABSTAIN"), **every("B2", "glaze", "ABSTAIN")}
        v = v6.verdict(main_jobs() + rec_jobs(res))
        self.assertEqual(v["outcome"], "REVERT")
        self.assertTrue(any("unique recall" in f for f in v["failed"]))
        self.assertEqual(v["outcome_without_unique_gate"], "KEEP")

    def test_recency_invented_reverts(self):
        v = v6.verdict(main_jobs() + rec_jobs({("B1", "recency-1", "badge"): "INVENTED"}))
        self.assertEqual(v["outcome"], "REVERT")

    def test_latency_gate(self):
        jobs = main_jobs()
        for j in jobs:  # make B much slower than A
            for b in j["blocks"]:
                if b["arm"] == "B":
                    for r in b["rows"]:
                        r["first_sentence_s"] = r["first_sentence_s"] + 100
        v = v6.verdict(jobs + rec_jobs())
        self.assertEqual(v["outcome"], "REVERT")
        self.assertTrue(any(f.startswith("p50") for f in v["failed"]))

    def test_main_revert_passes_through(self):
        jobs = main_jobs()
        jobs[0]["blocks"] = [copy.deepcopy(b) for b in jobs[0]["blocks"]]
        for b in jobs[0]["blocks"]:
            if b["label"] == "B1":
                for r in b["rows"]:
                    if r["current"] is not None:
                        r["result"] = "SUPERSEDED"
        v = v6.verdict(jobs + rec_jobs())
        self.assertEqual(v["outcome"], "REVERT")

    def test_null_on_missing_recency(self):
        self.assertEqual(v6.verdict(main_jobs() + rec_jobs()[:2])["outcome"], "NULL")
        jobs = rec_jobs()
        jobs[1]["complete"] = False
        self.assertEqual(v6.verdict(main_jobs() + jobs)["outcome"], "NULL")
        self.assertEqual(v6.verdict(main_jobs() + rec_jobs({("A2", "recency-2", "bench"): "ERROR"}))["outcome"], "NULL")
        self.assertEqual(v6.verdict(main_jobs()[:5] + rec_jobs())["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()
