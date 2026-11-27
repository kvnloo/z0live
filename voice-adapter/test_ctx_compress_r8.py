"""C1-ctx-compress-r8 harness: held-out r8 suite (sizes, slots, offline per-arm fact sets) and the frozen r8 verdict."""
import hashlib
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
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r8_verdict as v8  # noqa: E402

FIXTURE = HERE / "fixtures" / "ctx_compress_r4_jobs.json"
R7_PRODUCTION_SHA256 = "e22e84b64c76db340eccc98414000e8140872565b80f189e61869996a0f10b22"  # r7 freeze
CODE_KEYS = {"a1": ("a1_old", "a1_new"), "a2": ("a2_old", "a2_new"), "b1": ("b1_old", "b1_new"),
             "b2": ("b2_old", "b2_new"), "c": ("c_old", "c_new"), "d": ("d",)}
# Offline prediction (frozen in the PREREG and dry-r8.json): what each arm's context holds, per probe.
EXPECTED = {"A": {"a1": "STALE", "a2": "STALE", "d": "CURRENT", "b1": "STALE", "b2": "STALE",
                  "c_old": "CURRENT", "c_new": "ABSENT"},
            "B": {"a1": "CURRENT", "a2": "CURRENT", "d": "CURRENT", "b1": "STALE", "b2": "STALE",
                  "c_old": "ABSENT", "c_new": "CURRENT"},
            "O": {"a1": "BOTH", "a2": "BOTH", "d": "CURRENT", "b1": "BOTH", "b2": "BOTH",
                  "c_old": "CURRENT", "c_new": "CURRENT"}}


def exchanges(s):
    return brief_compress._split(r2.chat_brief(s).split("\n"))[1]


def find(ex, value):
    hits = [e for e in ex if value in e[0]]
    assert len(hits) == 1, value
    return hits[0]


class R8Suite(unittest.TestCase):
    def test_production_is_r7(self):
        self.assertEqual(hashlib.sha256((HERE / "brief_compress.py").read_bytes()).hexdigest(), R7_PRODUCTION_SHA256)

    def test_shape_and_sizes(self):
        suite = r8.r8_suite()
        self.assertEqual([s.name for s in suite], r8.R8_NAMES)
        self.assertEqual(sorted(r8.R8_NAMES), v8.R8)
        for i, (s, base) in enumerate(zip(suite, r2.suite())):
            self.assertEqual((s.stable, s.call), (base.stable, base.call))
            self.assertEqual(tuple(p.key for p in s.probes), r8.PROBE_ORDER)
            b = r2.chat_brief(s)
            self.assertLess(len(b), 6000)  # arm O under tiers.snapshot's cap
            self.assertGreater(len(b), r8.BUDGET)
            ex = exchanges(s)
            for e in ex:
                self.assertLessEqual(len(" ".join(e[1:])), brief_compress.FACT_REPLY_CHARS)  # every exchange a fact
                self.assertEqual(len(e), 2)
            c = r8.codes(i)
            size = lambda k: brief_compress._size([find(ex, c[k])])  # noqa: E731
            noise = [brief_compress._size([e]) for e in ex if " note " in e[0]]
            self.assertEqual(len(noise), r8.NOISE_PAIRS)
            olds = [size(k) for k in ("a1_old", "a2_old", "b1_old", "b2_old", "c_old", "d")]
            news = [size(k) for k in ("a1_new", "a2_new", "b1_new", "b2_new", "c_new")]
            self.assertLess(max(olds), min(noise))
            self.assertGreater(min(news), max(noise))
            self.assertLessEqual(max(news), min(olds) * 1.12)  # newer only slightly longer
            # every old fact precedes every new fact; the new facts are the newest exchanges
            pos = {k: ex.index(find(ex, c[k])) for k in c}
            self.assertLess(max(pos[k] for k in c if not k.endswith("_new")),
                            min(pos[k] for k in c if k.endswith("_new")))
            self.assertEqual(sorted(pos[k] for k in c if k.endswith("_new")), list(range(len(ex) - 5, len(ex))))

    def test_slots(self):
        r6_subjects = {"workshop bench number", "badge colour code", "glaze batch code"}
        for i, s in enumerate(r8.r8_suite()):
            ex, c = exchanges(s), r8.codes(i)
            slot = {k: brief_compress._slot(find(ex, c[k])) for k in c}
            for k in ("a1", "a2"):  # (a) transfer: same slot, subject unlike r6
                self.assertIsNotNone(slot[f"{k}_old"])
                self.assertEqual(slot[f"{k}_old"], slot[f"{k}_new"])
                self.assertNotIn(slot[f"{k}_old"], r6_subjects)
            for k in ("b1", "b2"):  # (b) rephrased update: no slot on the update
                self.assertIsNotNone(slot[f"{k}_old"])
                self.assertIsNone(slot[f"{k}_new"])
            self.assertEqual(slot["c_old"], slot["c_new"])  # (c) lexical false merge
            self.assertIsNotNone(slot["c_old"])
            olds = [slot[k] for k in c if not k.endswith("_new")]
            self.assertEqual(len(set(olds)), len(olds))  # old facts never share a slot with each other
            self.assertTrue(all(brief_compress._slot(e) is None for e in ex if " note " in e[0]))

    def test_dry_prediction(self):
        for row in r8.dry(r8.r8_suite()):
            for arm in "ABO":
                self.assertEqual(row[arm]["items"], EXPECTED[arm], (row["scenario"], arm))
            self.assertLessEqual(row["A"]["brief_chars"], r8.BUDGET)
            self.assertLessEqual(row["B"]["brief_chars"], r8.BUDGET)

    def test_scorer_on_false_merge_probe(self):
        p = next(p for p in r8.r8_suite()[0].probes if p.key == "c_old")
        c = r8.codes(0)
        self.assertEqual(r6.score_recency(p, c["c_old"]), "RECALL")
        self.assertEqual(r6.score_recency(p, c["c_new"]), "INVENTED")  # answered with the newer fact: LOSS
        self.assertEqual(r6.score_recency(p, "I don't know."), "ABSTAIN")

    def test_install(self):
        saved = (r2.COMPRESS, r2.dry, r2.suite, context_eval.score)
        try:
            r8.install("r8")
            self.assertEqual(r2.suite()[0].name, "r8-allotment")
            self.assertIs(context_eval.score, r6.score_recency)
            self.assertIs(r2.COMPRESS, r6.COMPRESS)
        finally:
            r2.COMPRESS, r2.dry, r2.suite, context_eval.score = saved


def main_jobs():
    jobs = json.loads(FIXTURE.read_text())["jobs"]
    for j in jobs:
        j["r8_set"] = "main"
    return jobs


def predicted(lab, key):
    """Gemma answers from the context: CURRENT -> CORRECT/RECALL, STALE -> STALE, ABSENT -> ABSTAIN."""
    want = EXPECTED[lab[0]][key]
    sup = key in ("a1", "a2", "b1", "b2")
    if want == "STALE":
        return "STALE"
    if want == "ABSENT":
        return "ABSTAIN"
    return "CORRECT" if sup else "RECALL"


def r8_jobs(results=None, base=predicted):
    results = results or {}
    jobs = []
    for s in r8.r8_suite():
        for idx in ((0, 1, 2), (3, 4, 5, 6)):
            blocks = []
            for lab in v8.LABELS:
                rows = [{"scenario": s.name, "key": s.probes[k].key, "result":
                         results.get((lab, s.name, s.probes[k].key), base(lab, s.probes[k].key)),
                         "first_sentence_s": 1.0} for k in idx]
                blocks.append({"label": lab, "arm": lab[0], "rows": rows})
            jobs.append({"r8_set": "r8", "scenarios": [s.name], "complete": True, "blocks": blocks})
    return jobs


def every(lab, key, res):
    return {(lab, n, key): res for n in r8.R8_NAMES}


class Verdict(unittest.TestCase):
    def test_dry_prediction_is_revert(self):
        v = v8.verdict(main_jobs() + r8_jobs())
        self.assertEqual(v["outcome"], "REVERT-r7", v["null_reasons"])
        self.assertEqual(v["same_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0})
        self.assertEqual(v["false_merge_loss"], {"A1": 0, "B1": 3, "A2": 0, "B2": 3, "O1": 0})
        self.assertTrue(v["checks"]["a_same_slot_stale_fixed"])
        self.assertEqual(v["reph_stale_report_only"]["B1"], 6)

    def test_no_false_merge_is_keep(self):
        res = {**every("B1", "c_old", "RECALL"), **every("B2", "c_old", "RECALL")}
        v = v8.verdict(main_jobs() + r8_jobs(res))
        self.assertEqual(v["outcome"], "KEEP-r7-generalises", v["checks"])

    def test_band_absorbs_loss(self):
        res = {**every("B1", "c_old", "RECALL"), ("B2", "r8-choir", "c_old"): "RECALL",
               ("A2", "r8-choir", "a1"): "CORRECT"}
        v = v8.verdict(main_jobs() + r8_jobs(res))
        self.assertEqual((v["r8"]["aa_flips"], v["r8"]["bb_flips"], v["band"]), (1, 2, 2))  # band = max(aa, bb)
        self.assertEqual(v["false_merge_loss"]["B2"], 2)
        self.assertNotEqual(v["outcome"], "REVERT-r7")  # min B 0 > max A 0 + 2 is false

    def test_stale_not_fixed_is_no_update(self):
        res = {**every("B1", "c_old", "RECALL"), **every("B2", "c_old", "RECALL"),
               **every("B1", "a1", "STALE"), **every("B2", "a1", "STALE"),
               **every("B1", "a2", "STALE"), **every("B2", "a2", "STALE")}
        v = v8.verdict(main_jobs() + r8_jobs(res))
        self.assertEqual(v["outcome"], "NO_UPDATE")

    def test_unique_loss_blocks_keep(self):
        res = {**every("B1", "c_old", "RECALL"), **every("B2", "c_old", "RECALL"),
               **every("B1", "d", "ABSTAIN"), **every("B2", "d", "ABSTAIN")}
        v = v8.verdict(main_jobs() + r8_jobs(res))
        self.assertEqual(v["outcome"], "NO_UPDATE")
        self.assertFalse(v["checks"]["d_unique_kept"])

    def test_null(self):
        self.assertEqual(v8.verdict(main_jobs() + r8_jobs()[:5])["outcome"], "NULL")
        jobs = r8_jobs()
        jobs[2]["complete"] = False
        self.assertEqual(v8.verdict(main_jobs() + jobs)["outcome"], "NULL")
        self.assertEqual(v8.verdict(main_jobs() + r8_jobs({("A2", "r8-cycling", "b1"): "ERROR"}))["outcome"], "NULL")
        self.assertEqual(v8.verdict(main_jobs()[:5] + r8_jobs())["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()
