"""C1-ctx-compress-r11 harness: r11 arms over the frozen r10 harness, the deterministic pre-check, presence and the
r11 verdict (frozen r10 rule + r11 harness check). No model."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import brief_compress_r9  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r10_verdict as v10  # noqa: E402
import ctx_compress_r11_eval as r11e  # noqa: E402
import ctx_compress_r11_verdict as v11  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402

# Offline prediction (frozen in the r11 PREREG): what each arm's context holds, same in all 3 scenarios of a set.
# Recency / r8: B = r11 equals r9 there (every superseder restates the value). r10: B keeps the old (e) facts as r4
# and fixes the (a) stale values as r9; no arm except O fits the newer (e) facts at 2810 (pre-check).
REC = {**t10.REC, "R": t10.REC["B"]}
R8 = {**t10.R8, "R": t10.R8["B"]}
R10 = {"A": t10.R10["A"],
       "B": {"a1": "CURRENT", "a2": "CURRENT", "d": "CURRENT", "e1_old": "CURRENT", "e2_old": "CURRENT",
             "e1_new": "ABSENT", "e2_new": "ABSENT"},
       "R": t10.R10["R"], "O": t10.R10["O"]}
ABSENT_BOTH = sorted(t10.ABSENT_BOTH + [f"{n}/{k}" for n in r10e.R10_NAMES for k in ("e1_new", "e2_new")])
PRES = r11e.presence()


def predicted(lab, key, name, missing="ABSTAIN"):
    table = REC if name.startswith("recency") else (R8 if name.startswith("r8-") else R10)
    want = table[lab[0]][key]
    if want == "STALE":
        return "STALE"
    if want == "ABSENT":
        return missing
    return "CORRECT" if key in t10.SUP else "RECALL"


def set_jobs(which, results=None, missing="ABSTAIN"):
    results = results or {}
    suite, halves = {"recency": (r6.recency_suite(), ((0, 1, 2),)),
                     "r8": (r8.r8_suite(), ((0, 1, 2), (3, 4, 5, 6))),
                     "r10": (r10e.r10_suite(), ((0, 1, 2, 3), (4, 5, 6)))}[which]
    jobs = []
    for s in suite:
        for idx in halves:
            blocks = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key),
                                       predicted(lab, s.probes[k].key, s.name, missing))}
                for k in idx]} for lab in v10.LABELS]
            jobs.append({"r10_set": which, "r11_harness": v11.HARNESS, "scenarios": [s.name], "complete": True,
                         "blocks": blocks})
    return jobs


def all_jobs(results=None, missing="ABSTAIN"):
    main = t10.main_jobs()
    for j in main:
        j["r11_harness"] = v11.HARNESS
    return main + sum((set_jobs(w, results, missing) for w in ("recency", "r8", "r10")), [])


class Harness(unittest.TestCase):
    def test_arms(self):
        self.assertEqual(sorted(r11e.COMPRESS), ["A", "B", "O", "R"])
        b = r2.chat_brief(r10e.r10_suite()[0])
        self.assertEqual(r11e.COMPRESS["A"](b), brief_compress_r4.compress_brief(b, 2810))
        self.assertEqual(r11e.COMPRESS["B"](b), brief_compress.compress_brief(b, 2810))
        self.assertEqual(r11e.COMPRESS["R"](b), brief_compress_r9.compress_brief(b, 2810))
        self.assertEqual(r11e.COMPRESS["O"](b), b)
        self.assertEqual(r11e.BUDGET, 2810)

    def test_presence_does_not_leak(self):
        before = r10e.COMPRESS
        r11e.presence()
        r11e.dry(r10e.r10_suite())
        self.assertIs(r10e.COMPRESS, before)

    def test_dry_prediction(self):
        for which, table in (("recency", REC), ("r8", R8), ("r10", R10)):
            for row in r11e.dry(r10e.SETS[which]()):
                for arm in "ABRO":
                    self.assertEqual(row[arm]["items"], table[arm], (row["scenario"], arm))

    def test_presence_map(self):
        self.assertEqual(len(PRES), 51)
        self.assertEqual(sorted(k for k, v in PRES.items() if not any(v.values())), ABSENT_BOTH)
        for n in r10e.R10_NAMES:
            self.assertEqual(PRES[f"{n}/e1_old"], {"A": True, "B": True})

    def test_precheck(self):
        pc = r11e.precheck()
        self.assertEqual(len(pc), 12)
        for name, row in pc.items():
            self.assertLessEqual(max(row["chars"].values()), 2810, name)
            if name.startswith("main/"):
                self.assertEqual(row["chars"]["r11"], row["chars"]["r4"])
            if name.startswith("r10/"):
                for k in ("e1", "e2"):
                    old, new = row["probes"][f"{k}_old"]["current"], row["probes"][f"{k}_new"]["current"]
                    self.assertEqual((old["r4"], old["r9"], old["r11"]), (True, False, True))
                    self.assertEqual((new["r4"], new["r9"], new["r11"]), (False, True, False))
                    self.assertFalse(old["r11"] and new["r11"])  # both facts never fit (budget-bound)


class Verdict(unittest.TestCase):
    def test_dry_prediction_keeps(self):
        v = v11.verdict(all_jobs(), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []), v["null_reasons"])
        self.assertEqual(v["r10_cfm_loss"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 6})
        self.assertEqual(v["r10a_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0, "R1": 0})
        self.assertEqual(v["presence_counts"]["absent_in_both"], 15)

    def test_invent_mode(self):
        # missing fact -> fixed invented code: e_new and r8 c_new/b are absent in both -> report-only
        v = v11.verdict(all_jobs(missing="INVENTED"), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["invented_present"]["B1"], 0)
        self.assertEqual(v["invented_absent_report_only"]["B1"], 6 + 3 + 0)

    def test_r9_like_result_reverts(self):
        res = {(lab, n, k): "ABSTAIN" for lab in ("B1", "B2") for n in r10e.R10_NAMES for k in ("e1_old", "e2_old")}
        v = v11.verdict(all_jobs(res), PRES)
        self.assertEqual(v["failed"], ["G1_r10_cfm_loss"])

    def test_invented_on_present_old_fact_is_gated(self):
        res = {(lab, n, "e1_old"): "INVENTED" for lab in ("B1", "B2") for n in r10e.R10_NAMES}
        v = v11.verdict(all_jobs(res), PRES)
        self.assertIn("G9_no_new_invented_present", v["failed"])

    def test_null_without_r11_meta(self):
        jobs = all_jobs()
        del jobs[-1]["r11_harness"]
        v = v11.verdict(jobs, PRES)
        self.assertEqual(v["outcome"], "NULL")
        self.assertEqual(v11.verdict([], PRES)["outcome"], "NULL")
        self.assertEqual(v11.verdict(all_jobs()[:-1], PRES)["outcome"], "NULL")

    def test_default_presence(self):
        self.assertEqual(v11.load_presence(), PRES)


if __name__ == "__main__":
    unittest.main()
