"""C1-ctx-compress-r12 held-out transfer set: loader, r11 attributes per item, deterministic pre-check / dry
prediction / presence, and the r12 verdict on mock jobs. No model."""
import copy
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import brief_compress_r7  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r12_eval as r12e  # noqa: E402
import ctx_compress_r12_verdict as v12  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402

# C1-ctx-compress-r13: production brief_compress.py moved on to r13. r12 scored B = r11, so these r12 tests pin the r12
# harness's B arm, its r11 pre-check arm and its attribute module to the r11 byte copy (bench/brief_compress_r11.py,
# sha256 f0233b21...) at import (PRES below is computed at import); restored after the module.
import brief_compress_r11  # noqa: E402
_SAVED = (r12e.COMPRESS, r12e.PRECHECK, r12e.brief_compress)
r12e.COMPRESS = {**r12e.COMPRESS, "B": lambda b: brief_compress_r11.compress_brief(b, r12e.BUDGET)}
r12e.PRECHECK = {**r12e.PRECHECK, "r11": brief_compress_r11}
r12e.brief_compress = brief_compress_r11


def tearDownModule():
    r12e.COMPRESS, r12e.PRECHECK, r12e.brief_compress = _SAVED

# Offline dry prediction (frozen in the r12 PREREG): what each arm's context holds. CURRENT = planted value in
# context, STALE = only the superseded value, ABSENT = neither. Same in all 3 scenarios except b (campsite "our" has
# no r7 slot, so r7 keeps b_old there).
_OLD = ("a_old", "b_old", "c_old", "d_old")
_NEW = ("a_new", "b_new", "c_new", "d_new")
A = {**{k: "CURRENT" for k in _OLD}, "e1": "STALE", "e2": "STALE", "f": "CURRENT", **{k: "ABSENT" for k in _NEW}}
B = {**A, "a_old": "ABSENT", "e1": "CURRENT", "e2": "CURRENT", "a_new": "CURRENT"}
R = {**{k: "ABSENT" for k in _OLD}, "e1": "CURRENT", "e2": "CURRENT", "f": "CURRENT",
     **{k: "CURRENT" for k in _NEW}}
O = {**{k: "CURRENT" for k in _OLD + _NEW}, "e1": "BOTH", "e2": "BOTH", "f": "CURRENT"}


def table(arm, name):
    t = {"A": A, "B": B, "R": R, "O": O}[arm]
    if name == "r12-campsite":
        t = {**t, "b_new": "ABSENT"} if arm == "R" else t
        t = {**t, "b_old": "CURRENT"} if arm == "R" else t
    return t


# r11 head per statement (frozen in the PREREG), scenario order campsite, coworking, leisure
HEADS = {"a_old": ("booked", "on", "registered"), "a_new": ("booked", "on", "registered"),
         "b_old": (None, "both", "members"), "b_new": (None, "in", "booked"),
         "c_old": ("kept", "set", "hanging"), "c_new": ("due", "booked", "waiting"),
         "d_old": ("orange", "silver", "red"), "d_new": ("parked", "registered", "in")}
PRES = r12e.presence()
HALVES = ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10))


def predicted(lab, key, name, missing="ABSTAIN"):
    want = table(lab[0], name)[key]
    if want == "STALE":
        return "STALE"
    if want == "ABSENT":
        return missing
    return "CORRECT" if key in r12e.SAME_KEYS else "RECALL"


def r12_jobs(results=None, missing="ABSTAIN"):
    results = results or {}
    jobs = []
    for s in r12e.r12_suite():
        for idx in HALVES:
            blocks = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key),
                                       predicted(lab, s.probes[k].key, s.name, missing))}
                for k in idx]} for lab in v12.LABELS]
            jobs.append({"r10_set": "r12", "r12_harness": v12.HARNESS, "scenarios": [s.name], "complete": True,
                         "blocks": blocks})
    return jobs


def all_jobs(results=None, missing="ABSTAIN"):
    main = t10.main_jobs()
    for j in main:
        j["r12_harness"] = v12.HARNESS
    return main + r12_jobs(results, missing)


class Loader(unittest.TestCase):
    def test_suite_shape(self):
        suite = r12e.r12_suite()
        self.assertEqual([s.name for s in suite], r12e.R12_NAMES)
        for s in suite:
            self.assertEqual(tuple(p.key for p in s.probes), r12e.PROBE_ORDER)
            self.assertEqual(len(s.chat) % 2, 0)

    def test_held_out_names_and_codes(self):
        for n in r12e.R12_NAMES:
            for old in ("workshop", "allotment", "cycling", "choir", "rowing", "climbing", "library"):
                self.assertNotIn(old, n)
        mine = {v.split("-")[0] for i in range(3) for v in r12e.codes(i).values()}
        earlier = set()
        for path in (HERE / "bench").glob("ctx_compress_r[0-9]*_eval.py"):
            if path.name != "ctx_compress_r12_eval.py":
                earlier |= set(re.findall(r'w\("([A-Z][a-z]+)"', path.read_text()))
        self.assertTrue(earlier)
        self.assertFalse(mine & earlier)
        vals = [v for i in range(3) for v in r12e.codes(i).values()]
        self.assertEqual(len(vals), len(set(vals)))

    def test_probe_values(self):
        for i, s in enumerate(r12e.r12_suite()):
            c, p = r12e.codes(i), {q.key: q for q in s.probes}
            for k in ("e1", "e2"):
                self.assertEqual((p[k].current, p[k].superseded), (c[f"{k}_new"], (c[f"{k}_old"],)))
            for k in _OLD + _NEW + ("f",):
                self.assertEqual((p[k].current, p[k].superseded), (c[k], ()))

    def test_sizes_old_lt_noise_lt_new(self):
        for s in r12e.r12_suite():
            _, exs, _ = brief_compress._split(r2.chat_brief(s).split("\n"))
            sizes = [brief_compress._size([x]) for x in exs]
            old, noise, new = sizes[:7], sizes[7:11], sizes[11:]
            self.assertEqual(len(new), 6)
            self.assertLess(max(old), min(noise), s.name)
            self.assertLess(max(noise), min(new), s.name)
            self.assertLessEqual(max(new), r12e.NEW_TARGET)

    def test_r11_heads_as_preregistered(self):
        for i in range(3):
            at = r12e.attributes(i)
            for role, heads in HEADS.items():
                a = at[role]["attribute"]
                self.assertEqual(None if a is None else a[1], heads[i], (i, role))
            for k in ("e1", "e2"):
                self.assertEqual(at[f"{k}_old"]["attribute"][1], brief_compress.VALUE)
                self.assertEqual(at[f"{k}_new"]["attribute"][1], brief_compress.VALUE)
                self.assertTrue(at[f"{k}_new"]["cue"])
            for p in "abcd":
                self.assertTrue(at[f"{p}_new"]["cue"], (i, p))
                self.assertFalse(at[f"{p}_old"]["cue"], (i, p))
        # (d): the only cue in the newer statement sits inside the value text
        for i, word in enumerate(("New", "Switched", "Now")):
            txt = r12e.statements(i)["d_new"]
            self.assertEqual([m.group(0) for m in brief_compress.CUE.finditer(txt)], [word])


class Prediction(unittest.TestCase):
    def test_arms(self):
        b = r2.chat_brief(r12e.r12_suite()[0])
        self.assertEqual(r12e.COMPRESS["A"](b), brief_compress_r4.compress_brief(b, 2810))
        self.assertEqual(r12e.COMPRESS["B"](b), brief_compress_r11.compress_brief(b, 2810))
        self.assertEqual(r12e.COMPRESS["R"](b), brief_compress_r7.compress_brief(b, 2810))
        self.assertEqual(r12e.COMPRESS["O"](b), b)

    def test_no_leak(self):
        before, sets = r10e.COMPRESS, dict(r10e.SETS)
        r12e.dry(r12e.r12_suite())
        r12e.presence()
        self.assertIs(r10e.COMPRESS, before)
        self.assertEqual(r10e.SETS, sets)

    def test_dry_prediction(self):
        for row in r12e.dry(r12e.r12_suite()):
            for arm in "ABRO":
                self.assertEqual(row[arm]["items"], table(arm, row["scenario"]), (row["scenario"], arm))
                self.assertLessEqual(row[arm]["brief_chars"], 2810 if arm != "O" else 10 ** 6)

    def test_presence_map(self):
        self.assertEqual(len(PRES), 33)
        absent = sorted(k for k, v in PRES.items() if not any(v.values()))
        self.assertEqual(absent, sorted(f"{n}/{k}" for n in r12e.R12_NAMES for k in ("b_new", "c_new", "d_new")))
        for n in r12e.R12_NAMES:
            self.assertEqual(PRES[f"{n}/a_old"], {"A": True, "B": False})
            self.assertEqual(PRES[f"{n}/a_new"], {"A": False, "B": True})

    def test_precheck(self):
        pc = r12e.precheck()
        self.assertEqual(len(pc), 6)
        for name, row in pc.items():
            self.assertLessEqual(max(row["chars"].values()), 2810, name)
            if name.startswith("main/"):
                self.assertEqual(row["chars"]["r11"], row["chars"]["r4"])

    def test_main_set_unchanged(self):
        self.assertIs(r12e.SETS["main"], r10e.SETS["main"])


class Verdict(unittest.TestCase):
    def test_dry_prediction_reverts_on_coinciding_heads(self):
        v = v12.verdict(all_jobs(), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("REVERT", ["G1_r12_fm_loss"]), v["null_reasons"])
        self.assertEqual(v["r12_fm_loss"], {"A1": 0, "B1": 3, "A2": 0, "B2": 3, "O1": 0, "R1": 11})
        self.assertEqual(v["fm_loss_bcd_report_only"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 8})
        self.assertEqual(v["r12_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0, "R1": 0})
        self.assertEqual(v["r12_unique_recall"]["B1"], 3)
        self.assertEqual(v["presence_counts"]["absent_in_both"], 9)
        self.assertTrue(v["checks"]["G7_latency"])

    def test_b_holds_old_facts_keeps(self):
        res = {(lab, n, "a_old"): "RECALL" for lab in ("B1", "B2") for n in r12e.R12_NAMES}
        v = v12.verdict(all_jobs(res), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []))

    def test_invent_mode(self):
        v = v12.verdict(all_jobs(missing="INVENTED"), PRES)
        # B's a_old is absent from B but present in A, so its invented answer is gated; symmetrically A's a_new
        # (present in B only) is gated too, so the gate sees parity 3 vs 3
        self.assertEqual(v["invented_present"]["B1"], 3)
        self.assertEqual(v["invented_present"]["A1"], 3)
        self.assertEqual(v["invented_absent_report_only"]["B1"], 9)
        self.assertNotIn("G6_no_new_invented_present", v["failed"])
        res = {(lab, n, "f"): "INVENTED" for lab in ("B1", "B2") for n in r12e.R12_NAMES}
        self.assertIn("G6_no_new_invented_present", v12.verdict(all_jobs(res), PRES)["failed"])

    def test_stale_not_fixed_reverts(self):
        res = {(lab, n, k): "STALE" for lab in ("B1", "B2") for n in r12e.R12_NAMES for k in ("e1", "e2")}
        self.assertIn("G2_r12_stale_fixed", v12.verdict(all_jobs(res), PRES)["failed"])

    def test_band_widens(self):
        res = {("A2", n, k): "ABSTAIN" for n in r12e.R12_NAMES for k in ("b_old", "c_old", "d_old")}
        v = v12.verdict(all_jobs(res), PRES)
        self.assertEqual(v["band"], 9)
        self.assertNotIn("G1_r12_fm_loss", v["failed"])

    def test_latency_gate(self):
        jobs = all_jobs()
        for j in jobs:
            for b in j["blocks"]:
                if j["r10_set"] == "main" and b["label"] in ("B1", "B2"):
                    for r in b["rows"]:
                        r["first_sentence_s"] = (r.get("first_sentence_s") or 0) * 2 + 100
        self.assertIn("G7_latency", v12.verdict(jobs, PRES)["failed"])

    def test_null(self):
        jobs = all_jobs()
        del jobs[-1]["r12_harness"]
        self.assertEqual(v12.verdict(jobs, PRES)["outcome"], "NULL")
        self.assertEqual(v12.verdict([], PRES)["outcome"], "NULL")
        self.assertEqual(v12.verdict(all_jobs()[:-1], PRES)["outcome"], "NULL")
        jobs = all_jobs()
        jobs[-1]["blocks"][0]["rows"][0]["result"] = "ERROR"
        self.assertEqual(v12.verdict(jobs, PRES)["outcome"], "NULL")
        jobs = copy.deepcopy(all_jobs())
        jobs[-1]["complete"] = False
        self.assertEqual(v12.verdict(jobs, PRES)["outcome"], "NULL")

    def test_default_presence(self):
        self.assertEqual(v12.load_presence(), PRES)

    def test_scorer_unchanged(self):
        p = r12e.r12_suite()[0].probes[4]
        self.assertEqual(r6.score_recency(p, p.current), "CORRECT")
        self.assertEqual(r6.score_recency(p, p.superseded[0]), "STALE")
        self.assertIs(r8.NEW_TARGET, r12e.NEW_TARGET)


if __name__ == "__main__":
    unittest.main()
