"""C1-ctx-compress-r10 harness: the r10 held-out set, the presence check, the frozen dry prediction and the r10 verdict."""
import json
import re
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
import ctx_compress_r10_eval as r10  # noqa: E402
import ctx_compress_r10_verdict as v10  # noqa: E402

FIXTURE = HERE / "fixtures" / "ctx_compress_r4_jobs.json"
# Offline prediction (frozen in the PREREG and dry-r10.json): what each arm's context holds per probe, identical in
# all 3 scenarios of each set. Recency and r8 rows are r9's (unchanged arms and sets).
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
_B10 = {"a1": "CURRENT", "a2": "CURRENT", "d": "CURRENT", "e1_old": "ABSENT", "e2_old": "ABSENT",
        "e1_new": "CURRENT", "e2_new": "CURRENT"}
R10 = {"A": {"a1": "STALE", "a2": "STALE", "d": "CURRENT", "e1_old": "CURRENT", "e2_old": "CURRENT",
             "e1_new": "ABSENT", "e2_new": "ABSENT"},
       "B": _B10, "R": _B10,
       "O": {"a1": "BOTH", "a2": "BOTH", "d": "CURRENT", "e1_old": "CURRENT", "e2_old": "CURRENT",
             "e1_new": "CURRENT", "e2_new": "CURRENT"}}
SUP = ("bench", "badge", "a1", "a2", "b1", "b2")
ABSENT_BOTH = sorted(f"{n}/{k}" for n in r8.R8_NAMES for k in ("b1", "b2", "c_new"))


def exchanges(s):
    t = s.chat
    return [[f"user: {t[k].text}", f"assistant: {t[k + 1].text}"] for k in range(0, len(t), 2)]


class Suite(unittest.TestCase):
    def test_arms_and_sets(self):
        self.assertEqual(sorted(r10.COMPRESS), ["A", "B", "O", "R"])
        self.assertEqual(r10.BUDGET, 2810)
        self.assertEqual(sorted(r10.SETS), ["main", "r10", "r8", "recency"])
        self.assertEqual([s.name for s in r10.r10_suite()], r10.R10_NAMES)

    def test_install(self):
        saved = (r2.COMPRESS, r2.dry, r2.suite, context_eval.score)
        try:
            for which, first, score in (("main", "workshop-1", r10.SCORE), ("recency", "recency-1", r6.score_recency),
                                        ("r8", "r8-allotment", r6.score_recency),
                                        ("r10", "r10-rowing", r6.score_recency)):
                r10.install(which)
                self.assertEqual(r2.suite()[0].name, first)
                self.assertIs(context_eval.score, score)
                self.assertIs(r2.COMPRESS, r10.COMPRESS)
        finally:
            r2.COMPRESS, r2.dry, r2.suite, context_eval.score = saved

    def test_sizes_old_lt_noise_lt_new(self):
        for s in r10.r10_suite():
            ex = exchanges(s)
            sizes = [brief_compress._size([e]) for e in ex]
            old, noise, new = sizes[:5], sizes[5:9], sizes[9:]
            self.assertLess(max(old), min(noise), s.name)
            self.assertLess(max(noise), min(new), s.name)
            self.assertLessEqual(max(new), r10.NEW_TARGET)

    def test_category_wording(self):
        """(e) the newer different-attribute fact shares the older fact's slot AND carries an r9 cue; (a) same; old
        facts and noise carry no cue; d has its own slot."""
        for s in r10.r10_suite():
            ex = exchanges(s)
            old, noise, new = ex[:5], ex[5:9], ex[9:]
            for e in old + noise:
                self.assertFalse(brief_compress._cue(e), e[0])
            slot = {brief_compress._slot(e) for e in old}
            self.assertEqual(len(slot), 5)  # five distinct slots among the older facts (a1 e1 d e2 a2)
            for e in new:
                self.assertIn(brief_compress._slot(e), slot, e[0])
                self.assertTrue(brief_compress._cue(e), e[0])
            self.assertTrue(all(brief_compress._slot(e) is None for e in noise))

    def test_codes_unique(self):
        brief_codes = set()
        for s in list(r10.R2_SUITE()) + list(r6.recency_suite()) + list(r8.r8_suite()):
            brief_codes |= set(re.findall(r"\b[A-Z][a-z]+-\d+\b", r2.chat_brief(s)))
        families = {c.split("-")[0] for c in brief_codes}
        for i in range(3):
            for v in r10.codes(i).values():
                self.assertNotIn(v.split("-")[0], families, v)
        self.assertEqual(len({v for i in range(3) for v in r10.codes(i).values()}), 27)

    def test_dry_prediction(self):
        for suite, want in ((r6.recency_suite(), REC), (r8.r8_suite(), R8), (r10.r10_suite(), R10)):
            for row in r10.dry(suite):
                for arm in "ABRO":
                    self.assertEqual(row[arm]["items"], want[arm], (row["scenario"], arm))
                for arm in "ABR":
                    self.assertLessEqual(row[arm]["brief_chars"], r10.BUDGET)

    def test_main_set_briefs_identical(self):
        for row in r10.dry(r10.R2_SUITE()):
            self.assertEqual(row["A"], row["B"])
            self.assertEqual(row["A"], row["R"])


class Presence(unittest.TestCase):
    def test_presence_map(self):
        pres = r10.presence()
        self.assertEqual(len(pres), 9 + 21 + 21)
        self.assertEqual(sorted(k for k, v in pres.items() if not any(v.values())), ABSENT_BOTH)
        for n in r10.R10_NAMES:
            for k in ("e1_old", "e2_old"):
                self.assertEqual(pres[f"{n}/{k}"], {"A": True, "B": False})
            for k in ("e1_new", "e2_new", "a1", "a2"):
                self.assertEqual(pres[f"{n}/{k}"], {"A": False, "B": True})
            self.assertEqual(pres[f"{n}/d"], {"A": True, "B": True})
        for n in r8.R8_NAMES:
            self.assertEqual(pres[f"{n}/c_old"], {"A": True, "B": True})

    def test_presence_is_brief_substring(self):
        s = r10.r10_suite()[0]
        brief = r2.chat_brief(s)
        p = s.probes[r10.PROBE_ORDER.index("e1_old")]
        self.assertIn(p.current, r10.COMPRESS["A"](brief))
        self.assertNotIn(p.current, r10.COMPRESS["B"](brief))
        self.assertEqual(r10.presence(("O",))[f"{s.name}/e1_old"], {"O": True})

    def test_presence_deterministic(self):
        self.assertEqual(json.dumps(r10.presence()), json.dumps(r10.presence()))


PRES = r10.presence()


def main_jobs():
    jobs = json.loads(FIXTURE.read_text())["jobs"]
    for j in jobs:
        j["r10_set"] = "main"
    return jobs


def predicted(lab, key, name):
    """Gemma answers from the context: CURRENT -> CORRECT/RECALL, STALE -> STALE, ABSENT -> ABSTAIN."""
    table = REC if name.startswith("recency") else (R8 if name.startswith("r8-") else R10)
    want = table[lab[0]][key]
    if want == "STALE":
        return "STALE"
    if want == "ABSENT":
        return "ABSTAIN"
    return "CORRECT" if key in SUP else "RECALL"


def set_jobs(which, results=None):
    results = results or {}
    suite, halves = {"recency": (r6.recency_suite(), ((0, 1, 2),)),
                     "r8": (r8.r8_suite(), ((0, 1, 2), (3, 4, 5, 6))),
                     "r10": (r10.r10_suite(), ((0, 1, 2, 3), (4, 5, 6)))}[which]
    jobs = []
    for s in suite:
        for idx in halves:
            blocks = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key), predicted(lab, s.probes[k].key, s.name))}
                for k in idx]} for lab in v10.LABELS]
            jobs.append({"r10_set": which, "scenarios": [s.name], "complete": True, "blocks": blocks})
    return jobs


def all_jobs(results=None):
    return main_jobs() + set_jobs("recency", results) + set_jobs("r8", results) + set_jobs("r10", results)


def every(lab, key, res, names=r10.R10_NAMES):
    return {(lab, n, key): res for n in names}


def both_b(key, res, names=r10.R10_NAMES):
    return {**every("B1", key, res, names), **every("B2", key, res, names)}


class Verdict(unittest.TestCase):
    def test_dry_prediction_reverts_on_primary(self):
        v = v10.verdict(all_jobs(), PRES)
        self.assertEqual(v["outcome"], "REVERT", v["null_reasons"])
        self.assertEqual(v["failed"], ["G1_r10_cfm_loss"])
        self.assertEqual(v["r10_cfm_loss"], {"A1": 0, "B1": 6, "A2": 0, "B2": 6, "O1": 0, "R1": 6})
        self.assertEqual(v["r10a_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0, "R1": 0})
        self.assertEqual(v["r8_fm_loss"]["B1"], 0)
        self.assertEqual(v["band"], 0)
        self.assertEqual(v["presence_counts"]["absent_in_both"], 9)

    def test_attribute_aware_would_keep(self):
        # a rule keeping both different-attribute facts: B recalls e_old -> every gate passes
        res = {**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL")}
        v = v10.verdict(all_jobs(res), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []))

    def test_r9_g7_case_is_report_only(self):
        # r9's G7 failure: B invents on r8 c_new, absent from both briefs -> report-only in r10
        res = {**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL"),
               **both_b("c_new", "INVENTED", r8.R8_NAMES)}
        v = v10.verdict(all_jobs(res), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["invented_absent_report_only"]["B1"], 3)
        self.assertEqual(v["invented_present"]["B1"], 0)

    def test_invented_on_present_fact_is_gated(self):
        # B invents where the fact is in the A brief (e_old): counts in G9
        res = {**both_b("e1_old", "INVENTED"), **both_b("e2_old", "RECALL")}
        v = v10.verdict(all_jobs(res), PRES)
        self.assertEqual(v["invented_present"]["B1"], 3)
        self.assertIn("G9_no_new_invented_present", v["failed"])
        self.assertIn("G1_r10_cfm_loss", v["failed"])
        # A inventing on a probe present only in B (e_new) also counts on A's side
        res.update({(lab, n, "e1_new"): "INVENTED" for lab in ("A1", "A2") for n in r10.R10_NAMES})
        v = v10.verdict(all_jobs(res), PRES)
        self.assertTrue(v["checks"]["G9_no_new_invented_present"])

    def test_stale_transfer_gate(self):
        res = {**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL"), **both_b("a1", "STALE"),
               **both_b("a2", "STALE")}
        self.assertEqual(v10.verdict(all_jobs(res), PRES)["failed"], ["G5_r10a_stale_fixed"])

    def test_unique_and_r9_gates_carried(self):
        base = {**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL")}
        v = v10.verdict(all_jobs({**base, **both_b("d", "ABSTAIN")}), PRES)
        self.assertEqual(v["failed"], ["G6_unique_kept"])
        v = v10.verdict(all_jobs({**base, **both_b("c_old", "ABSTAIN", r8.R8_NAMES)}), PRES)
        self.assertEqual(v["failed"], ["G2_r8_fm_loss"])
        v = v10.verdict(all_jobs({**base, **both_b("bench", "STALE", r6.RECENCY_NAMES)}), PRES)
        self.assertEqual(v["failed"], [])  # B STALE 3 < A 6 - 0: a partial fix still passes
        v = v10.verdict(all_jobs({**base, **both_b("bench", "STALE", r6.RECENCY_NAMES),
                                  **both_b("badge", "STALE", r6.RECENCY_NAMES)}), PRES)
        self.assertEqual(v["failed"], ["G3_recency_stale_fixed"])

    def test_band_pools_r10(self):
        res = {**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL"),
               ("A2", "r10-library", "d"): "ABSTAIN", ("B1", "r10-rowing", "e1_new"): "ABSTAIN"}
        del res[("B2", "r10-rowing", "e1_old")]  # B2 keeps the predicted ABSTAIN -> one B/B flip
        v = v10.verdict(all_jobs(res), PRES)
        self.assertEqual((v["flips"]["r10_aa"], v["flips"]["r10_bb"], v["band"]), (1, 2, 2))
        self.assertEqual(v["r10_cfm_loss"]["B2"], 1)
        self.assertEqual(v["outcome"], "KEEP")  # max B 1 <= max A 0 + band 2

    def test_rival_report_only(self):
        v = v10.verdict(all_jobs({**both_b("e1_old", "RECALL"), **both_b("e2_old", "RECALL"),
                                  **every("R1", "a1", "ERROR")}), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["r10"]["counts"]["R1"]["SAME"]["ERROR"], 3)

    def test_null(self):
        jobs = all_jobs()
        self.assertEqual(v10.verdict(jobs[:-1], PRES)["outcome"], "NULL")
        self.assertEqual(v10.verdict(main_jobs() + set_jobs("recency") + set_jobs("r8"), PRES)["outcome"], "NULL")
        self.assertEqual(v10.verdict(all_jobs({("A2", "r10-library", "e2_new"): "ERROR"}), PRES)["outcome"], "NULL")
        jobs[-1]["complete"] = False
        self.assertEqual(v10.verdict(jobs, PRES)["outcome"], "NULL")
        pres = dict(PRES)
        del pres["r10-rowing/d"]
        v = v10.verdict(all_jobs(), pres)
        self.assertEqual(v["outcome"], "NULL")
        self.assertIn("no presence entry for r10-rowing/d", v["null_reasons"])

    def test_default_presence_is_harness(self):
        self.assertEqual(v10.load_presence(), PRES)


if __name__ == "__main__":
    unittest.main()
