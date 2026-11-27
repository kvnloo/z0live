"""C1-ctx-compress-r13: attribute key v2 (the ONE change), the r13 held-out set, deterministic pre-check / dry
prediction / presence, and the r13 verdict on mock jobs. No model."""
import copy
import hashlib
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
# C1-ctx-compress-r14: production brief_compress.py moved on to r14 (label tie-break). These r13 tests pin the r13 rule
# to its byte copy (bench/brief_compress_r13.py = brief_compress.py at 4ec7dfd, sha256 d788b84a...), and the r13
# harness's B arm, r13 pre-check arm and attribute module to that copy at import (PRES below is computed at import);
# restored after the module.
import brief_compress_r13 as bc  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_r11 as r11  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r12_eval as r12e  # noqa: E402
import ctx_compress_r13_eval as r13e  # noqa: E402
import ctx_compress_r13_verdict as v13  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

_SAVED = (r13e.COMPRESS, r13e.PRECHECK, r13e.brief_compress)
r13e.COMPRESS = {**r13e.COMPRESS, "B": lambda b: bc.compress_brief(b, r13e.BUDGET)}
r13e.PRECHECK = {**r13e.PRECHECK, "r13": bc}
r13e.brief_compress = bc


def tearDownModule():
    r13e.COMPRESS, r13e.PRECHECK, r13e.brief_compress = _SAVED

R4_SHA256 = "5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64"
R11_SHA256 = "f0233b21d0b4c82bd83e3c48a3c508d282f01ffbd7cd8199b44db625b9617a05"  # brief_compress.py at 3dcf56c
V = bc.VALUE


def key(text):
    return bc._attribute([f"user: {text}", "assistant: ok"])


def turns_with(s, value):
    """User turn texts of scenario s that contain value."""
    return [t.text for t in s.chat if t.role == "user" and value in t.text]


# ---------------------------------------------------------------------------------------------------------------
# Offline dry prediction for the r13 set (frozen in the r13 PREREG): what each arm's context holds. CURRENT = planted
# value in context, STALE = only the superseded value, BOTH = both, ABSENT = neither. Same in all 3 scenarios.
_OLD2 = ("a2x_old", "a2y_old")
_NEW2 = ("a2x_new", "a2y_new")
DRY = {
    "A": {**{k: "CURRENT" for k in _OLD2}, "e1": "STALE", "e2": "STALE", "f": "CURRENT", "a3_old": "CURRENT",
          "e3": "STALE", **{k: "ABSENT" for k in _NEW2}, "a3_new": "ABSENT"},
    "B": {**{k: "CURRENT" for k in _OLD2}, "e1": "CURRENT", "e2": "CURRENT", "f": "CURRENT", "a3_old": "ABSENT",
          "e3": "STALE", **{k: "ABSENT" for k in _NEW2}, "a3_new": "CURRENT"},
    "R": {**{k: "ABSENT" for k in _OLD2}, "e1": "CURRENT", "e2": "CURRENT", "f": "CURRENT", "a3_old": "ABSENT",
          "e3": "CURRENT", **{k: "CURRENT" for k in _NEW2}, "a3_new": "CURRENT"},
    "O": {**{k: "CURRENT" for k in _OLD2 + _NEW2}, "e1": "BOTH", "e2": "BOTH", "f": "CURRENT", "a3_old": "CURRENT",
          "e3": "BOTH", "a3_new": "CURRENT"},
}
# v2 keys of the r13 statements, scenario order marina, hotel, festival (frozen in the PREREG)
R13_KEYS = {
    "a2x_old": (("boat", "moored", "at"), ("suitcase", "left", "at"), ("camper van", "parked", "by")),
    "a2x_new": (("boat", "moored", "for"), ("suitcase", "left", "with"), ("camper van", "parked", "for")),
    "a2y_old": (("dinghy", "stored", "in"), ("rental car", "parked", "in"), ("wristband", "linked", "weekend")),
    "a2y_new": (("dinghy", "stored", "on"), ("rental car", "parked", "for"), ("wristband", "linked", "with")),
    "a3_old": (("paddleboard", "booked", "for"), ("room key", "linked", "with"), ("friend group", "registered", "for")),
    "e1_old": (("mooring fee ref", V), ("wifi code", V), ("locker code", V)),
    "e2_old": (("fuel card", "number", V), ("room", "number", V), ("shuttle seat", "number", V)),
    "e3_old": (("tender", "on", "north"), ("bike", "in", "front"), ("flag", "on", "left")),
    "e3_new": (("tender", "on", "south"), ("bike", "in", "back"), ("flag", "on", "right")),
    "f": (("harbour gate fob code", V), ("minibar card code", V), ("first aid badge code", V)),
}
R13_KEYS["a3_new"] = R13_KEYS["a3_old"]
R13_KEYS["e1_new"] = R13_KEYS["e1_old"]
R13_KEYS["e2_new"] = R13_KEYS["e2_old"]
# v2 keys of the r12 (a) statements: same r11 head word, different second word
R12_A_KEYS = {"a_old": (("van", "booked", "for"), ("locker", "on", "second"), ("car", "registered", "for")),
              "a_new": (("van", "booked", "in"), ("locker", "on", "keypad"), ("car", "registered", "with"))}

PRES = r13e.presence()
# job layout of the frozen workflow: (set, blocks, probe index groups)
LAYOUT = {"r13": ("B,A,B,A,O,R", ((0, 1, 2, 3, 4), (5, 6, 7, 8, 9))),
          "r12": ("B,A,B,A", ((0, 1, 2, 3, 4, 5, 6),)),
          "recency": ("B,A,B,A", ((0, 1, 2),)),
          "r8": ("B,A,B,A", ((0, 1, 2, 5),)),
          "r10": ("B,A,B,A", ((0, 1, 2, 3, 4),))}
_DRY = {w: {row["scenario"]: row for row in r13e.dry(r13e.SETS[w]())} for w in LAYOUT}


def predicted(which, lab, name, k, missing="ABSTAIN"):
    want = _DRY[which][name][lab[0]]["items"][k]
    p = next(q for q in r13e.SETS[which]() if q.name == name).probes
    sup = bool(next(q for q in p if q.key == k).superseded)
    return {"STALE": "STALE", "BOTH": "BOTH", "ABSENT": missing}.get(want, "CORRECT" if sup else "RECALL")


def set_jobs(which, results=None, missing="ABSTAIN"):
    results = results or {}
    blocks, groups = LAYOUT[which]
    jobs = []
    for s in r13e.SETS[which]():
        for idx in groups:
            labs, seen = [], {}
            for a in blocks.split(","):
                seen[a] = seen.get(a, 0) + 1
                labs.append(f"{a}{seen[a]}")
            bl = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key),
                                       predicted(which, lab, s.name, s.probes[k].key, missing))}
                for k in idx]} for lab in labs]
            jobs.append({"r10_set": which, "r13_harness": v13.HARNESS, "scenarios": [s.name], "complete": True,
                         "blocks": bl})
    return jobs


def all_jobs(results=None, missing="ABSTAIN"):
    main = t10.main_jobs()
    for j in main:
        j["r13_harness"] = v13.HARNESS
    return main + [j for w in LAYOUT for j in set_jobs(w, results, missing)]


class KeyV2(unittest.TestCase):
    def test_key_examples(self):
        self.assertEqual(key("My van is booked for the ferry on Friday, the booking ref is Jade-21."),
                         ("van", "booked", "for"))
        self.assertEqual(key("My van is now booked in at the new garage, the job card is Onyx-52."), ("van", "booked", "in"))
        self.assertEqual(key("My pitch number is Quartz-14."), ("pitch number", V))
        self.assertEqual(key("My pitch number is Agate-45 now, the warden moved us."), ("pitch number", V))
        self.assertEqual(key("My gate code is changed to Jasper-67 from today."), ("gate code", V))
        self.assertEqual(key("My belay card code is no longer the old one, it is Holly-74."), ("belay card code", V))
        self.assertEqual(key("My fuel card is number Spica-31."), ("fuel card", "number", V))
        self.assertEqual(key("My fuel card is now number Antares-68, the last one expired."), ("fuel card", "number", V))
        self.assertEqual(key("My dog is happy"), ("dog", "happy"))
        self.assertEqual(key("My dog is"), ("dog", V))
        self.assertIsNone(key("Our tent is the green one tagged Topaz-17."))
        self.assertEqual(bc.KEY_WORDS, 2)
        self.assertEqual((bc.FILLER, bc.CUE.pattern, bc.WORD.pattern, bc.VALUE),
                         (r11.FILLER, r11.CUE.pattern, r11.WORD.pattern, r11.VALUE))

    def test_head_is_r11_head(self):
        """The first key word is r11's head, so v2 only ever splits r11 groups (never merges new ones)."""
        for i in range(3):
            for txt in list(r12e.statements(i).values()) + list(r13e.statements(i).values()):
                a, h = key(txt), r11._attribute([f"user: {txt}"])
                self.assertEqual(None if a is None else a[:2], h, txt)

    def test_r12_items(self):
        for i in range(3):
            st = r12e.statements(i)
            for role, ks in R12_A_KEYS.items():
                self.assertEqual(key(st[role]), ks[i], (i, role))
            for p in "abcd":  # (a)-(d): v2 keys differ (b campsite has no slot)
                o, n = key(st[f"{p}_old"]), key(st[f"{p}_new"])
                if o is not None:
                    self.assertNotEqual(o, n, (i, p))
            for k in ("e1", "e2"):  # (e): same key, newer one cued
                self.assertEqual(key(st[f"{k}_old"]), key(st[f"{k}_new"]), (i, k))
                self.assertEqual(key(st[f"{k}_old"])[1:], (V,))
            self.assertEqual(key(st["f"])[1:], (V,))
            # the r11 heads of (a) coincide: the r12 failure v2 targets
            self.assertEqual(r11._attribute([f"user: {st['a_old']}"]), r11._attribute([f"user: {st['a_new']}"]))

    def test_r13_items(self):
        for i in range(3):
            st = r13e.statements(i)
            for role, ks in R13_KEYS.items():
                self.assertEqual(key(st[role]), ks[i], (i, role))
            for role, txt in st.items():
                self.assertEqual(bc._cue([f"user: {txt}"]), role.endswith("_new"), (i, role))
            for p in ("a2x", "a2y", "e3"):  # same r11 head, different v2 key
                o, n = f"{p}_old", f"{p}_new"
                self.assertEqual(r11._attribute([f"user: {st[o]}"]), r11._attribute([f"user: {st[n]}"]))
                self.assertNotEqual(key(st[o]), key(st[n]))

    def test_recency_r8_r10_items(self):
        """Stale-update pairs keep one v2 key (supersede); false-merge pairs have different keys (keep both)."""
        pairs = {"recency": ("bench", "badge"), "r8": ("a1", "a2"), "r10": ("a1", "a2")}
        for which, ks in pairs.items():
            for s in r13e.SETS[which]():
                for p in (q for q in s.probes if q.key in ks):
                    (old,), (new,) = turns_with(s, p.superseded[0]), turns_with(s, p.current)
                    self.assertIsNotNone(key(old), (s.name, p.key))
                    self.assertEqual(key(old), key(new), (s.name, p.key))
                    self.assertTrue(bc._cue([f"user: {new}"]), (s.name, p.key))
        fm = {"r8": (("c_old", "c_new"),), "r10": (("e1_old", "e1_new"), ("e2_old", "e2_new"))}
        for which, prs in fm.items():
            for s in r13e.SETS[which]():
                pr = {q.key: q for q in s.probes}
                for o, n in prs:
                    (to,), (tn,) = turns_with(s, pr[o].current), turns_with(s, pr[n].current)
                    self.assertNotEqual(key(to), key(tn), (s.name, o))


class Production(unittest.TestCase):
    def test_byte_copies(self):
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r4.py").read_bytes()).hexdigest(), R4_SHA256)
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r11.py").read_bytes()).hexdigest(),
                         R11_SHA256)

    def test_r13_equals_r4_on_main(self):
        for s in r10e.SETS["main"]():
            b = r2.chat_brief(s)
            self.assertEqual(bc.compress_brief(b, 2810), r4.compress_brief(b, 2810), s.name)
        for text in capitalised_briefs():
            for b in BUDGETS:
                self.assertEqual(bc.compress_brief(text, b), r4.compress_brief(text, b), b)

    def test_r13_equals_r11_where_keys_agree(self):
        for which in ("recency", "r8", "r10"):
            for s in r13e.SETS[which]():
                b = r2.chat_brief(s)
                self.assertEqual(bc.compress_brief(b, 2810), r11.compress_brief(b, 2810), s.name)

    def test_v2_splits_r11_group(self):
        ex = [["User: My van is booked for the ferry, ref Jade-1.", "Assistant: ok"],
              ["User: My van is now booked in at the garage, job card Onyx-2. " + "x" * 40, "Assistant: ok"]]
        self.assertEqual(r11._fact_order(ex, [0, 1]), [1])  # r11 drops the older true fact
        self.assertEqual(bc._fact_order(ex, [0, 1]), [0, 1])  # v2: different attribute, r4 order
        ex2 = [["User: My gate code is Oak-1.", "Assistant: ok"],
               ["User: My gate code is changed to Oak-2 from today, the warden said.", "Assistant: ok"]]
        self.assertEqual(bc._fact_order(ex2, [0, 1]), [1])  # genuine update still supersedes


class Loader(unittest.TestCase):
    def test_suite_shape(self):
        suite = r13e.r13_suite()
        self.assertEqual([s.name for s in suite], r13e.R13_NAMES)
        for s in suite:
            self.assertEqual(tuple(p.key for p in s.probes), r13e.PROBE_ORDER)
            self.assertEqual(len(s.chat), 2 * (7 + 4 + 6))

    def test_held_out_names_and_codes(self):
        for n in r13e.R13_NAMES:
            for old in ("workshop", "allotment", "cycling", "choir", "rowing", "climbing", "library", "campsite",
                        "coworking", "leisure"):
                self.assertNotIn(old, n)
        mine = {v.split("-")[0] for i in range(3) for v in r13e.codes(i).values()}
        earlier = set()
        for path in (HERE / "bench").glob("ctx_compress_r[0-9]*_eval.py"):
            if path.name != "ctx_compress_r13_eval.py":
                earlier |= set(re.findall(r'w\("([A-Z][a-z]+)"', path.read_text()))
        self.assertTrue({"Jade", "Poplar", "Wren"} <= earlier)
        self.assertFalse(mine & earlier)
        stable = " ".join(t.text for s in r10e.SETS["main"]() for t in s.chat)
        for fam in mine:
            self.assertNotIn(fam + "-", stable)
        vals = [v for i in range(3) for v in r13e.codes(i).values()]
        self.assertEqual(len(vals), len(set(vals)))

    def test_probe_values(self):
        for i, s in enumerate(r13e.r13_suite()):
            c, p = r13e.codes(i), {q.key: q for q in s.probes}
            for k in ("e1", "e2", "e3"):
                self.assertEqual((p[k].current, p[k].superseded), (c[f"{k}_new"], (c[f"{k}_old"],)))
            for k in _OLD2 + _NEW2 + ("a3_old", "a3_new", "f"):
                self.assertEqual((p[k].current, p[k].superseded), (c[k], ()))

    def test_sizes_old_lt_noise_lt_new(self):
        for s in r13e.r13_suite():
            _, exs, _ = bc._split(r2.chat_brief(s).split("\n"))
            sizes = [bc._size([x]) for x in exs]
            old, noise, new = sizes[:7], sizes[7:11], sizes[11:]
            self.assertEqual(len(new), 6)
            self.assertLess(max(old), min(noise), s.name)
            self.assertLess(max(noise), min(new), s.name)
            self.assertLessEqual(max(new), r13e.NEW_TARGET)

    def test_frozen_counts(self):
        self.assertEqual({w: sum(len(v) for v in v13.SETS[w]["cats"].values()) for w in v13.SETS},
                         {"r13": 10, "r12": 8, "recency": 3, "r8": 4, "r10": 5})
        self.assertEqual(len(PRES), 9 + 21 + 21 + 33 + 30)
        scored = {w: sum(len(g) for g in LAYOUT[w][1]) for w in LAYOUT}
        self.assertEqual(scored, {"r13": 10, "r12": 7, "recency": 3, "r8": 4, "r10": 5})
        for w, (_, groups) in LAYOUT.items():  # every gated key is scored, and only the declared ones
            for s in r13e.SETS[w]():
                keys = {s.probes[k].key for g in groups for k in g}
                want = {k for c, ks in v13.SETS[w]["cats"].items() for k in ks}
                self.assertEqual(keys, want, (w, s.name))
                self.assertTrue(set(r13e.STALE_KEYS[w]) | set(r13e.OLD_KEYS[w]) <= keys)


class Prediction(unittest.TestCase):
    def test_arms(self):
        b = r2.chat_brief(r13e.r13_suite()[0])
        self.assertEqual(r13e.COMPRESS["A"](b), r4.compress_brief(b, 2810))
        self.assertEqual(r13e.COMPRESS["B"](b), bc.compress_brief(b, 2810))
        self.assertEqual(r13e.COMPRESS["R"](b), r11.compress_brief(b, 2810))
        self.assertEqual(r13e.COMPRESS["O"](b), b)

    def test_no_leak(self):
        before, sets = r10e.COMPRESS, dict(r10e.SETS)
        r13e.dry(r13e.r13_suite())
        r13e.presence()
        self.assertIs(r10e.COMPRESS, before)
        self.assertEqual(r10e.SETS, sets)

    def test_dry_prediction(self):
        for row in r13e.dry(r13e.r13_suite()):
            for arm in "ABRO":
                self.assertEqual(row[arm]["items"], DRY[arm], (row["scenario"], arm))
                self.assertLessEqual(row[arm]["brief_chars"], 2810 if arm != "O" else 10 ** 6)

    def test_precheck_passes(self):
        pc = r13e.precheck()
        self.assertEqual(pc["stop"], [])
        self.assertEqual(pc["new_set_failures"], [])
        self.assertEqual(len(pc["briefs"]), 18)
        for name, row in pc["briefs"].items():
            self.assertLessEqual(max(row["chars"].values()), 2810, name)
            if name.startswith("main/"):
                self.assertTrue(row["r13_eq_r4"], name)

    def test_presence_map(self):
        for n in r13e.R13_NAMES:
            self.assertEqual(PRES[f"{n}/a3_old"], {"A": True, "B": False})
            self.assertEqual(PRES[f"{n}/a3_new"], {"A": False, "B": True})
            for k in _OLD2 + ("f",):
                self.assertEqual(PRES[f"{n}/{k}"], {"A": True, "B": True})
            for k in _NEW2 + ("e3",):
                self.assertEqual(PRES[f"{n}/{k}"], {"A": False, "B": False})
        for n in r12e.R12_NAMES:
            self.assertEqual(PRES[f"{n}/a_old"], {"A": True, "B": True})


class Verdict(unittest.TestCase):
    def test_dry_prediction_keeps(self):
        v = v13.verdict(all_jobs(), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []), v["null_reasons"])
        self.assertEqual(v["r13_fm_loss"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 6})
        self.assertEqual(v["r12_fm_loss"]["B1"], 0)
        for w in v13.PROBE_SETS:
            self.assertEqual((v["stale"][w]["A1"], v["stale"][w]["B1"]), (6, 0), w)
            self.assertEqual(v["unique_recall"][w]["B1"], 3, w)
        self.assertEqual(v["r13_a3_loss_report_only"]["B1"], 3)
        self.assertEqual(v["r13_e3_stale_report_only"], {"A1": 3, "B1": 3, "A2": 3, "B2": 3, "O1": 0, "R1": 0})
        self.assertEqual(v["presence_counts"]["scored_probes"], 87)
        self.assertEqual(v["presence_counts"]["absent_in_both"], 9)
        self.assertEqual(v["band"], 0)

    def test_r13_loss_reverts(self):
        res = {(lab, n, "a2x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES}
        self.assertEqual(v13.verdict(all_jobs(res), PRES)["failed"], ["G1_r13_fm_loss"])

    def test_r12_loss_reverts(self):
        res = {(lab, n, "a_old"): "INVENTED" for lab in ("B1", "B2") for n in r12e.R12_NAMES}
        self.assertIn("G1b_r12_fm_loss", v13.verdict(all_jobs(res), PRES)["failed"])

    def test_rerun_loss_reverts(self):
        res = {(lab, n, "c_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r8.R8_NAMES}
        self.assertEqual(v13.verdict(all_jobs(res), PRES)["failed"], ["G1c_rerun_fm_loss"])

    def test_limits_report_only(self):
        res = {(lab, n, k): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES for k in ("a3_old", "a2x_new")}
        res.update({(lab, n, "e3"): "STALE" for lab in ("B1", "B2") for n in r13e.R13_NAMES})
        self.assertEqual(v13.verdict(all_jobs(res), PRES)["outcome"], "KEEP")

    def test_stale_on_any_set_reverts(self):
        for w, ks, names in (("r13", ("e1", "e2"), r13e.R13_NAMES), ("recency", ("bench", "badge"), r6.RECENCY_NAMES),
                             ("r10", ("a1", "a2"), r10e.R10_NAMES)):
            res = {(lab, n, k): "STALE" for lab in ("B1", "B2") for n in names for k in ks}
            v = v13.verdict(all_jobs(res), PRES)
            self.assertIn("G2_stale_fixed", v["failed"], w)
            self.assertFalse(v["stale_ok"][w])

    def test_unique_reverts(self):
        res = {(lab, n, "d"): "ABSTAIN" for lab in ("B1", "B2") for n in r10e.R10_NAMES}
        self.assertIn("G3_unique_kept", v13.verdict(all_jobs(res), PRES)["failed"])

    def test_invent_mode(self):
        v = v13.verdict(all_jobs(missing="INVENTED"), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual((v["invented_present"]["A1"], v["invented_present"]["B1"]), (3, 3))
        self.assertEqual(v["invented_absent_report_only"]["B1"], 6)
        res = {(lab, n, "f"): "INVENTED" for lab in ("B1", "B2") for n in r13e.R13_NAMES}
        self.assertIn("G6_no_new_invented_present", v13.verdict(all_jobs(res), PRES)["failed"])

    def test_band_widens(self):
        res = {("A2", n, k): "ABSTAIN" for n in r12e.R12_NAMES for k in ("b_old", "c_old")}
        res.update({(lab, n, "a2x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES})
        v = v13.verdict(all_jobs(res), PRES)
        self.assertEqual(v["band"], 6)
        self.assertNotIn("G1_r13_fm_loss", v["failed"])
        self.assertIn("G2_stale_fixed", v["failed"])  # 0 < 6 - 6 is false: a wide band also blocks KEEP

    def test_latency_gate(self):
        jobs = all_jobs()
        for j in jobs:
            for b in j["blocks"]:
                if j["r10_set"] == "main" and b["label"] in ("B1", "B2"):
                    for r in b["rows"]:
                        r["first_sentence_s"] = (r.get("first_sentence_s") or 0) * 2 + 100
        self.assertIn("G7_latency", v13.verdict(jobs, PRES)["failed"])

    def test_null(self):
        jobs = all_jobs()
        del jobs[-1]["r13_harness"]
        self.assertEqual(v13.verdict(jobs, PRES)["outcome"], "NULL")
        self.assertEqual(v13.verdict([], PRES)["outcome"], "NULL")
        self.assertEqual(v13.verdict(all_jobs()[:-1], PRES)["outcome"], "NULL")
        jobs = all_jobs()
        jobs[-1]["blocks"][0]["rows"][0]["result"] = "ERROR"
        self.assertEqual(v13.verdict(jobs, PRES)["outcome"], "NULL")
        jobs = copy.deepcopy(all_jobs())
        jobs[-1]["complete"] = False
        self.assertEqual(v13.verdict(jobs, PRES)["outcome"], "NULL")
        jobs = [j for j in all_jobs() if j["r10_set"] != "r8"]
        self.assertEqual(v13.verdict(jobs, PRES)["outcome"], "NULL")
        pres = dict(PRES)
        del pres["r13-hotel/f"]
        self.assertEqual(v13.verdict(all_jobs(), pres)["outcome"], "NULL")

    def test_default_presence(self):
        self.assertEqual(v13.load_presence(), PRES)

    def test_scorer_unchanged(self):
        p = r13e.r13_suite()[0].probes[2]
        self.assertEqual(r6.score_recency(p, p.current), "CORRECT")
        self.assertEqual(r6.score_recency(p, p.superseded[0]), "STALE")
        self.assertIs(r8.NEW_TARGET, r13e.NEW_TARGET)


if __name__ == "__main__":
    unittest.main()
