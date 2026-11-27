"""C1-ctx-compress-r14: label tie-break on attribute key v2 (the ONE change), the r14 held-out set, deterministic
pre-check / dry prediction / presence, and the r14 verdict on mock jobs. No model."""
import copy
import hashlib
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress as bc  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_r11 as r11  # noqa: E402
import brief_compress_r13 as r13  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r12_eval as r12e  # noqa: E402
import ctx_compress_r13_eval as r13e  # noqa: E402
import ctx_compress_r14_eval as r14e  # noqa: E402
import ctx_compress_r14_verdict as v14  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

R4_SHA256 = "5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64"
R11_SHA256 = "f0233b21d0b4c82bd83e3c48a3c508d282f01ffbd7cd8199b44db625b9617a05"  # brief_compress.py at 3dcf56c
R13_SHA256 = "d788b84ad42a0bd4cd654ee5cebdc9a0e370d48b57204d6b492a014208f28600"  # brief_compress.py at 4ec7dfd
V, L = bc.VALUE, bc.LABEL


def key(text, mod=bc):
    return mod._attribute([f"user: {text}", "assistant: ok"])


def turns_with(s, value):
    """User turn texts of scenario s that contain value."""
    return [t.text for t in s.chat if t.role == "user" and value in t.text]


# ---------------------------------------------------------------------------------------------------------------
# Offline dry prediction for the r14 set (frozen in the r14 PREREG): what each arm's context holds. CURRENT = planted
# value in context, STALE = only the superseded value, BOTH = both, ABSENT = neither. Same in all 3 scenarios.
_OLD = ("a3x_old", "a3y_old")
_NEW = ("a3x_new", "a3y_new")
_UPD = ("e3x", "e3y", "e1", "e2")
DRY = {
    "A": {**{k: "CURRENT" for k in _OLD}, **{k: "STALE" for k in _UPD}, "f": "CURRENT", **{k: "ABSENT" for k in _NEW}},
    "B": {**{k: "CURRENT" for k in _OLD}, **{k: "CURRENT" for k in _UPD}, "f": "CURRENT",
          **{k: "ABSENT" for k in _NEW}},
    "R": {**{k: "ABSENT" for k in _OLD}, "e3x": "STALE", "e3y": "STALE", "e1": "CURRENT", "e2": "CURRENT",
          "f": "CURRENT", **{k: "CURRENT" for k in _NEW}},
    "O": {**{k: "CURRENT" for k in _OLD + _NEW}, **{k: "BOTH" for k in _UPD}, "f": "CURRENT"},
}
# r14 keys of the r14 statements, scenario order airport, ski, theatre (frozen in the PREREG)
R14_KEYS = {
    "a3x_old": (("bag", "checked", L, "bag", "tag"), ("skis", "booked", L, "race", "bib"),
                ("umbrella", "left", L, "box", "slip")),
    "a3x_new": (("bag", "checked", L, "claim", "ref"), ("skis", "booked", L, "wax", "ticket"),
                ("umbrella", "left", L, "bar", "token")),
    "a3y_old": (("ticket", "linked", L, "boarding", "pass"), ("lift pass", "loaded", L, "ride", "ref"),
                ("booking", "paired", L, "dinner", "ref")),
    "a3y_new": (("ticket", "linked", L, "lounge", "pass"), ("lift pass", "loaded", L, "meal", "code"),
                ("booking", "paired", L, "tour", "code")),
    "e3x_old": (("coat", "at", L, "hook", "tag"), ("boots", "in", L, "locker", "tag"), ("scarf", "in", L, "peg", "tag")),
    "e3y_old": (("pram", "with", L, "pram", "tag"), ("sledge", "by", L, "sledge", "tag"),
                ("bike", "on", L, "rail", "tag")),
    "e1_old": (("parking code", V), ("chalet code", V), ("interval drinks code", V)),
    "e2_old": (("seat", "number", L, "number"), ("class", "number", L, "number"), ("row", "number", L, "number")),
    "f": (("lounge voucher code", V), ("first aid card code", V), ("cast meet pass code", V)),
}
for _k in ("e3x", "e3y", "e1", "e2"):
    R14_KEYS[f"{_k}_new"] = R14_KEYS[f"{_k}_old"]
# r13 v2 keys of the r14 (a3) / (e3) statements: (a3) coincide (v2 false merge), (e3) differ (v2 stale)
R14_V2 = {"a3x": (("bag", "checked", "through"), ("skis", "booked", "for"), ("umbrella", "left", "at")),
          "a3y": (("ticket", "linked", "with"), ("lift pass", "loaded", "with"), ("booking", "paired", "with")),
          "e3x": ((("coat", "at", "west"), ("coat", "at", "east")), (("boots", "in", "upper"), ("boots", "in", "lower")),
                  (("scarf", "in", "stalls"), ("scarf", "in", "circle"))),
          "e3y": ((("pram", "with", "gate"), ("pram", "with", "cabin")), (("sledge", "by", "top"), ("sledge", "by", "base")),
                  (("bike", "on", "side"), ("bike", "on", "rear")))}

PRES = r14e.presence()
# job layout of the frozen workflow: (set, blocks, probe index groups)
LAYOUT = {"r14": ("B,A,B,A,O,R", ((0, 1, 2, 3, 4), (5, 6, 7, 8))),
          "r13": ("B,A,B,A", ((0, 1, 2, 3, 4, 5, 6),)),
          "r12": ("B,A,B,A", ((0, 1, 2, 3, 4, 5, 6),)),
          "recency": ("B,A,B,A", ((0, 1, 2),)),
          "r8": ("B,A,B,A", ((0, 1, 2, 5),)),
          "r10": ("B,A,B,A", ((0, 1, 2, 3, 4),))}
_DRY = {w: {row["scenario"]: row for row in r14e.dry(r14e.SETS[w]())} for w in LAYOUT}


def predicted(which, lab, name, k, missing="ABSTAIN"):
    want = _DRY[which][name][lab[0]]["items"][k]
    p = next(q for q in r14e.SETS[which]() if q.name == name).probes
    sup = bool(next(q for q in p if q.key == k).superseded)
    return {"STALE": "STALE", "BOTH": "BOTH", "ABSENT": missing}.get(want, "CORRECT" if sup else "RECALL")


def set_jobs(which, results=None, missing="ABSTAIN"):
    results = results or {}
    blocks, groups = LAYOUT[which]
    jobs = []
    for s in r14e.SETS[which]():
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
            jobs.append({"r10_set": which, "r14_harness": v14.HARNESS, "scenarios": [s.name], "complete": True,
                         "blocks": bl})
    return jobs


def all_jobs(results=None, missing="ABSTAIN"):
    main = t10.main_jobs()
    for j in main:
        j["r14_harness"] = v14.HARNESS
    return main + [j for w in LAYOUT for j in set_jobs(w, results, missing)]


class KeyR14(unittest.TestCase):
    def test_label_examples(self):
        lab = bc._value_label
        self.assertEqual(lab(" moored at the east pontoon, the berth tag is Vega-22."), ("berth", "tag"))
        self.assertEqual(lab(" number Spica-31."), ("number",))
        self.assertEqual(lab(" now number Antares-68, the last one expired."), ("number",))
        self.assertEqual(lab(" Quartz-14."), ())
        self.assertEqual(lab(" changed to Jasper-67 from today."), ())
        self.assertEqual(lab(" Agate-45 now, the warden moved us."), ())
        self.assertEqual(lab(" no longer the old one, it is Holly-74."), ())
        self.assertEqual(lab(" the yellow one tagged Rowan-33."), ("yellow", "tagged"))
        self.assertEqual(lab(" happy"), ())
        self.assertEqual((bc.LABEL_WORDS, bc.KEY_WORDS), (2, 2))
        self.assertEqual((bc.FILLER, bc.CUE.pattern, bc.WORD.pattern, bc.VALUE, bc.SLOT.pattern),
                         (r13.FILLER, r13.CUE.pattern, r13.WORD.pattern, r13.VALUE, r13.SLOT.pattern))

    def test_key_examples(self):
        # a3: same head and second word, different label -> two attributes (r13 merged them)
        o = "My paddleboard is booked for the harbour tour, the tour ref is Sirius-27."
        n = "My paddleboard is now booked for the new repair slot, the repair ticket is Castor-58."
        self.assertEqual(key(o, r13), key(n, r13))
        self.assertNotEqual(key(o), key(n))
        # e3: same head, different second word, same label -> one attribute (r13 kept the stale value)
        o = "My tender is on the north jetty, the jetty tag is Pollux-24."
        n = "My tender is now on the south jetty, the jetty tag is Mira-75."
        self.assertNotEqual(key(o, r13), key(n, r13))
        self.assertEqual(key(o), key(n))
        self.assertEqual(key(o), ("tender", "on", L, "jetty", "tag"))
        # no label: v2 key unchanged
        self.assertEqual(key("My pitch number is Quartz-14."), ("pitch number", V))
        self.assertEqual(key("My gate code is changed to Jasper-67 from today."), ("gate code", V))
        self.assertEqual(key("My dog is happy"), ("dog", "happy"))
        self.assertEqual(key("My dog is"), ("dog", V))
        self.assertIsNone(key("Our tent is the green one tagged Topaz-17."))

    def test_head_is_r11_head(self):
        """The first key word is r11's head, so r14 only ever splits r11 groups (never merges across them)."""
        for i in range(3):
            for txt in (list(r12e.statements(i).values()) + list(r13e.statements(i).values())
                        + list(r14e.statements(i).values())):
                a, h = key(txt), r11._attribute([f"user: {txt}"])
                self.assertEqual(None if a is None else a[:2], h, txt)

    def test_r14_items(self):
        for i in range(3):
            st = r14e.statements(i)
            for role, ks in R14_KEYS.items():
                self.assertEqual(key(st[role]), ks[i], (i, role))
            for role, txt in st.items():
                self.assertEqual(bc._cue([f"user: {txt}"]), role.endswith("_new"), (i, role))
            for p in ("a3x", "a3y"):  # r13 merges (same v2 key + cue), r14 keeps both
                o, n = st[f"{p}_old"], st[f"{p}_new"]
                self.assertEqual(key(o, r13), R14_V2[p][i])
                self.assertEqual(key(n, r13), R14_V2[p][i])
                self.assertNotEqual(key(o), key(n))
            for p in ("e3x", "e3y"):  # r13 keeps the stale value (v2 keys differ), r14 supersedes
                o, n = st[f"{p}_old"], st[f"{p}_new"]
                self.assertEqual((key(o, r13), key(n, r13)), R14_V2[p][i])
                self.assertEqual(key(o), key(n))
            for p in ("e1", "e2"):  # normal same-attribute updates: one key for r13 and r14
                self.assertEqual(key(st[f"{p}_old"], r13), key(st[f"{p}_new"], r13))
                self.assertEqual(key(st[f"{p}_old"]), key(st[f"{p}_new"]))

    def test_r13_items(self):
        """On the r13 set, r14 changes exactly the two v2 limits: a3 split, e3 merged; every other pair as r13."""
        for i in range(3):
            st = r13e.statements(i)
            self.assertNotEqual(key(st["a3_old"]), key(st["a3_new"]))
            self.assertEqual(key(st["e3_old"]), key(st["e3_new"]))
            for p in ("a2x", "a2y"):
                self.assertNotEqual(key(st[f"{p}_old"]), key(st[f"{p}_new"]), (i, p))
            for p in ("e1", "e2"):
                self.assertEqual(key(st[f"{p}_old"]), key(st[f"{p}_new"]), (i, p))
            roles = sorted(st)
            for x in roles:  # same grouping as r13 for every pair of statements except a3 and e3
                for y in roles:
                    if {x[:2], y[:2]} in ({"a3"}, {"e3"}):
                        continue
                    self.assertEqual(key(st[x]) == key(st[y]), key(st[x], r13) == key(st[y], r13), (i, x, y))

    def test_r12_items_grouping_unchanged(self):
        for i in range(3):
            st = r12e.statements(i)
            for x in st:
                for y in st:
                    self.assertEqual(key(st[x]) == key(st[y]), key(st[x], r13) == key(st[y], r13), (i, x, y))

    def test_recency_r8_r10_items(self):
        """Stale-update pairs keep one r14 key (supersede); false-merge pairs have different keys (keep both)."""
        pairs = {"recency": ("bench", "badge"), "r8": ("a1", "a2"), "r10": ("a1", "a2")}
        for which, ks in pairs.items():
            for s in r14e.SETS[which]():
                for p in (q for q in s.probes if q.key in ks):
                    (old,), (new,) = turns_with(s, p.superseded[0]), turns_with(s, p.current)
                    self.assertIsNotNone(key(old), (s.name, p.key))
                    self.assertEqual(key(old), key(new), (s.name, p.key))
                    self.assertTrue(bc._cue([f"user: {new}"]), (s.name, p.key))
        fm = {"r8": (("c_old", "c_new"),), "r10": (("e1_old", "e1_new"), ("e2_old", "e2_new"))}
        for which, prs in fm.items():
            for s in r14e.SETS[which]():
                pr = {q.key: q for q in s.probes}
                for o, n in prs:
                    (to,), (tn,) = turns_with(s, pr[o].current), turns_with(s, pr[n].current)
                    self.assertNotEqual(key(to), key(tn), (s.name, o))


class Production(unittest.TestCase):
    def test_byte_copies(self):
        for name, sha in (("brief_compress_r4.py", R4_SHA256), ("brief_compress_r11.py", R11_SHA256),
                          ("brief_compress_r13.py", R13_SHA256)):
            self.assertEqual(hashlib.sha256((HERE / "bench" / name).read_bytes()).hexdigest(), sha, name)

    def test_r14_equals_r4_on_main(self):
        for s in r10e.SETS["main"]():
            b = r2.chat_brief(s)
            self.assertEqual(bc.compress_brief(b, 2810), r4.compress_brief(b, 2810), s.name)
        for text in capitalised_briefs():
            for b in BUDGETS:
                self.assertEqual(bc.compress_brief(text, b), r4.compress_brief(text, b), b)

    def test_r14_equals_r13_on_dev_sets(self):
        for which in r14e.SAME_AS_R13:
            for s in r14e.SETS[which]():
                b = r2.chat_brief(s)
                self.assertEqual(bc.compress_brief(b, 2810), r13.compress_brief(b, 2810), s.name)

    def test_fact_order(self):
        a3 = [["User: My paddleboard is booked for the harbour tour, the tour ref is Sirius-27.", "Assistant: ok"],
              ["User: My paddleboard is now booked for the new repair slot, the repair ticket is Castor-58. "
               + "x" * 40, "Assistant: ok"]]
        self.assertEqual(r13._fact_order(a3, [0, 1]), [1])  # r13 drops the older true fact
        self.assertEqual(bc._fact_order(a3, [0, 1]), [0, 1])  # r14: different label, r4 order
        e3 = [["User: My tender is on the north jetty, the jetty tag is Pollux-24.", "Assistant: ok"],
              ["User: My tender is now on the south jetty, the jetty tag is Mira-75. " + "x" * 40, "Assistant: ok"]]
        self.assertEqual(r13._fact_order(e3, [0, 1]), [0, 1])  # r13 keeps the stale value too
        self.assertEqual(bc._fact_order(e3, [0, 1]), [1])  # r14 supersedes
        ex2 = [["User: My gate code is Oak-1.", "Assistant: ok"],
               ["User: My gate code is changed to Oak-2 from today, the warden said.", "Assistant: ok"]]
        self.assertEqual(bc._fact_order(ex2, [0, 1]), [1])  # genuine update still supersedes


class Loader(unittest.TestCase):
    def test_suite_shape(self):
        suite = r14e.r14_suite()
        self.assertEqual([s.name for s in suite], r14e.R14_NAMES)
        for s in suite:
            self.assertEqual(tuple(p.key for p in s.probes), r14e.PROBE_ORDER)
            self.assertEqual(len(s.chat), 2 * (7 + 4 + 6))

    def test_held_out_names_and_codes(self):
        for n in r14e.R14_NAMES:
            for old in ("workshop", "allotment", "cycling", "choir", "rowing", "climbing", "library", "campsite",
                        "coworking", "leisure", "marina", "hotel", "festival"):
                self.assertNotIn(old, n)
        mine = {v.split("-")[0] for i in range(3) for v in r14e.codes(i).values()}
        earlier = set()
        for path in (HERE / "bench").glob("ctx_compress_r[0-9]*_eval.py"):
            if path.name != "ctx_compress_r14_eval.py":
                earlier |= set(re.findall(r'w\("([A-Z][a-z]+)"', path.read_text()))
        self.assertTrue({"Jade", "Poplar", "Wren", "Vega", "Polaris"} <= earlier)
        self.assertFalse(mine & earlier)
        stable = " ".join(t.text for s in r10e.SETS["main"]() for t in s.chat)
        for fam in mine:
            self.assertNotIn(fam + "-", stable)
        vals = [v for i in range(3) for v in r14e.codes(i).values()]
        self.assertEqual(len(vals), len(set(vals)))

    def test_probe_values(self):
        for i, s in enumerate(r14e.r14_suite()):
            c, p = r14e.codes(i), {q.key: q for q in s.probes}
            for k in _UPD:
                self.assertEqual((p[k].current, p[k].superseded), (c[f"{k}_new"], (c[f"{k}_old"],)))
            for k in _OLD + _NEW + ("f",):
                self.assertEqual((p[k].current, p[k].superseded), (c[k], ()))

    def test_sizes_old_lt_noise_lt_new(self):
        for s in r14e.r14_suite():
            _, exs, _ = bc._split(r2.chat_brief(s).split("\n"))
            sizes = [bc._size([x]) for x in exs]
            old, noise, new = sizes[:7], sizes[7:11], sizes[11:]
            self.assertEqual(len(new), 6)
            self.assertLessEqual(max(old), r14e.OLD_TARGET, s.name)
            self.assertLess(max(old), min(noise), s.name)
            self.assertLess(max(noise), min(new), s.name)
            self.assertLessEqual(max(new), r14e.NEW_TARGET)

    def test_frozen_counts(self):
        self.assertEqual({w: sum(len(v) for v in v14.SETS[w]["cats"].values()) for w in v14.SETS},
                         {"r14": 9, "r13": 7, "r12": 8, "recency": 3, "r8": 4, "r10": 5})
        self.assertEqual(len(PRES), 9 + 21 + 21 + 33 + 30 + 27)
        scored = {w: sum(len(g) for g in LAYOUT[w][1]) for w in LAYOUT}
        self.assertEqual(scored, {"r14": 9, "r13": 7, "r12": 7, "recency": 3, "r8": 4, "r10": 5})
        for w, (_, groups) in LAYOUT.items():  # every gated key is scored, and only the declared ones
            for s in r14e.SETS[w]():
                keys = {s.probes[k].key for g in groups for k in g}
                want = {k for c, ks in v14.SETS[w]["cats"].items() for k in ks if c != "FM_CO"}
                self.assertEqual(keys, want, (w, s.name))
                self.assertTrue(set(r14e.STALE_KEYS[w]) | set(r14e.OLD_KEYS[w]) <= keys)


class Prediction(unittest.TestCase):
    def test_arms(self):
        b = r2.chat_brief(r14e.r14_suite()[0])
        self.assertEqual(r14e.COMPRESS["A"](b), r4.compress_brief(b, 2810))
        self.assertEqual(r14e.COMPRESS["B"](b), bc.compress_brief(b, 2810))
        self.assertEqual(r14e.COMPRESS["R"](b), r13.compress_brief(b, 2810))
        self.assertEqual(r14e.COMPRESS["O"](b), b)
        self.assertEqual(set(r14e.COMPRESS), set("ABRO"))  # V is offline only, never a model block
        self.assertIsNot(r14e.brief_compress_vshape, r13)
        self.assertIsNot(r14e.brief_compress_vshape._attribute, r13._attribute)

    def test_no_leak(self):
        before, sets = r10e.COMPRESS, dict(r10e.SETS)
        r14e.dry(r14e.r14_suite())
        r14e.presence()
        self.assertIs(r10e.COMPRESS, before)
        self.assertEqual(r10e.SETS, sets)

    def test_dry_prediction(self):
        for row in r14e.dry(r14e.r14_suite()):
            for arm in "ABRO":
                self.assertEqual(row[arm]["items"], DRY[arm], (row["scenario"], arm))
                self.assertLessEqual(row[arm]["brief_chars"], 2810 if arm != "O" else 10 ** 6)

    def test_dry_r13_limits_fixed(self):
        for row in r14e.dry(r13e.r13_suite()):
            self.assertEqual((row["B"]["items"]["a3_old"], row["B"]["items"]["e3"]), ("CURRENT", "CURRENT"))
            self.assertEqual((row["R"]["items"]["a3_old"], row["R"]["items"]["e3"]), ("ABSENT", "STALE"))

    def test_precheck_passes(self):
        pc = r14e.precheck()
        self.assertEqual(pc["stop"], [])
        self.assertEqual(pc["new_set_failures"], [])
        self.assertEqual(len(pc["briefs"]), 21)
        for name, row in pc["briefs"].items():
            self.assertLessEqual(max(row["chars"].values()), 2810, name)
            if name.startswith("main/"):
                self.assertTrue(row["r14_eq_r4"], name)
            if name.split("/")[0] in r14e.SAME_AS_R13:
                self.assertTrue(row["r14_eq_r13"], name)

    def test_presence_map(self):
        for n in r14e.R14_NAMES:
            for k in _OLD + ("f",) + _UPD:
                self.assertTrue(PRES[f"{n}/{k}"]["B"], (n, k))
            for k in _OLD + ("f",):
                self.assertTrue(PRES[f"{n}/{k}"]["A"], (n, k))
            for k in _NEW:
                self.assertEqual(PRES[f"{n}/{k}"], {"A": False, "B": False})
        for n in r13e.R13_NAMES:
            self.assertEqual(PRES[f"{n}/a3_old"], {"A": True, "B": True})
            self.assertEqual(PRES[f"{n}/e3"], {"A": False, "B": True})

    def test_value_shape_rival_offline(self):
        """V (report-only, offline): all planted values share one shape, so V merges every same-head pair: it drops
        every r14 a3 old fact and fixes every e3."""
        pv = r14e.presence(("A", "B", "V"))
        for n in r14e.R14_NAMES:
            for k in _OLD:
                self.assertFalse(pv[f"{n}/{k}"]["V"], (n, k))
            for k in ("e3x", "e3y"):
                self.assertTrue(pv[f"{n}/{k}"]["V"], (n, k))


class Verdict(unittest.TestCase):
    def test_dry_prediction_keeps(self):
        v = v14.verdict(all_jobs(), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []), v["null_reasons"])
        self.assertEqual(v["r14_a3_loss"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 6})
        self.assertEqual(v["r14_e3_stale"], {"A1": 6, "B1": 0, "A2": 6, "B2": 0, "O1": 0, "R1": 6})
        self.assertEqual(v["r13_fm_loss"]["B1"], 0)
        self.assertEqual(v["r12_fm_loss"]["B1"], 0)
        for w in v14.PROBE_SETS:
            self.assertEqual((v["stale"][w]["A1"], v["stale"][w]["B1"]), (6, 0), w)
            self.assertEqual(v["unique_recall"][w]["B1"], 3, w)
        self.assertEqual(v["r13_a3_loss_report_only"]["B1"], 0)
        self.assertEqual(v["r13_e3_stale_report_only"]["B1"], 0)
        self.assertEqual(v["presence_counts"]["scored_probes"], 105)
        self.assertEqual(v["presence_counts"]["absent_in_both"], 6)
        self.assertEqual(v["band"], 0)

    def test_a3_loss_reverts(self):
        res = {(lab, n, "a3x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r14e.R14_NAMES}
        self.assertEqual(v14.verdict(all_jobs(res), PRES)["failed"], ["G1_r14_a3_loss"])

    def test_e3_stale_reverts(self):
        res = {(lab, n, "e3x"): "STALE" for lab in ("B1", "B2") for n in r14e.R14_NAMES}
        res.update({(lab, n, "e3y"): "STALE" for lab in ("B1", "B2") for n in r14e.R14_NAMES})
        self.assertEqual(v14.verdict(all_jobs(res), PRES)["failed"], ["G1e_r14_e3_stale"])

    def test_r13_loss_reverts(self):
        res = {(lab, n, "a2x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES}
        self.assertEqual(v14.verdict(all_jobs(res), PRES)["failed"], ["G1b_r13_fm_loss"])

    def test_r12_loss_reverts(self):
        res = {(lab, n, "a_old"): "INVENTED" for lab in ("B1", "B2") for n in r12e.R12_NAMES}
        self.assertIn("G1c_r12_fm_loss", v14.verdict(all_jobs(res), PRES)["failed"])

    def test_rerun_loss_reverts(self):
        res = {(lab, n, "c_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r8.R8_NAMES}
        self.assertEqual(v14.verdict(all_jobs(res), PRES)["failed"], ["G1d_rerun_fm_loss"])

    def test_report_only(self):
        res = {(lab, n, k): "ABSTAIN" for lab in ("B1", "B2") for n in r14e.R14_NAMES for k in _NEW}
        res.update({(lab, n, "a3_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES})
        res.update({(lab, n, "e3"): "STALE" for lab in ("B1", "B2") for n in r13e.R13_NAMES})
        self.assertEqual(v14.verdict(all_jobs(res), PRES)["outcome"], "KEEP")

    def test_stale_on_any_set_reverts(self):
        for w, ks, names in (("r14", ("e1", "e2"), r14e.R14_NAMES), ("r13", ("e1", "e2"), r13e.R13_NAMES),
                             ("recency", ("bench", "badge"), r6.RECENCY_NAMES), ("r10", ("a1", "a2"), r10e.R10_NAMES)):
            res = {(lab, n, k): "STALE" for lab in ("B1", "B2") for n in names for k in ks}
            v = v14.verdict(all_jobs(res), PRES)
            self.assertIn("G2_stale_fixed", v["failed"], w)
            self.assertFalse(v["stale_ok"][w])

    def test_unique_reverts(self):
        res = {(lab, n, "f"): "ABSTAIN" for lab in ("B1", "B2") for n in r14e.R14_NAMES}
        self.assertIn("G3_unique_kept", v14.verdict(all_jobs(res), PRES)["failed"])

    def test_invent_mode(self):
        v = v14.verdict(all_jobs(missing="INVENTED"), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["invented_present"]["A1"], v["invented_present"]["B1"])
        res = {(lab, n, "f"): "INVENTED" for lab in ("B1", "B2") for n in r14e.R14_NAMES}
        self.assertIn("G6_no_new_invented_present", v14.verdict(all_jobs(res), PRES)["failed"])

    def test_band_widens(self):
        res = {("A2", n, k): "ABSTAIN" for n in r12e.R12_NAMES for k in ("b_old", "c_old")}
        res.update({(lab, n, "a3x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r14e.R14_NAMES})
        v = v14.verdict(all_jobs(res), PRES)
        self.assertEqual(v["band"], 6)
        self.assertNotIn("G1_r14_a3_loss", v["failed"])
        self.assertIn("G1e_r14_e3_stale", v["failed"])  # 0 < 6 - 6 is false: a wide band also blocks KEEP
        self.assertIn("G2_stale_fixed", v["failed"])

    def test_latency_gate(self):
        jobs = all_jobs()
        for j in jobs:
            for b in j["blocks"]:
                if j["r10_set"] == "main" and b["label"] in ("B1", "B2"):
                    for r in b["rows"]:
                        r["first_sentence_s"] = (r.get("first_sentence_s") or 0) * 2 + 100
        self.assertIn("G7_latency", v14.verdict(jobs, PRES)["failed"])

    def test_null(self):
        jobs = all_jobs()
        del jobs[-1]["r14_harness"]
        self.assertEqual(v14.verdict(jobs, PRES)["outcome"], "NULL")
        self.assertEqual(v14.verdict([], PRES)["outcome"], "NULL")
        self.assertEqual(v14.verdict(all_jobs()[:-1], PRES)["outcome"], "NULL")
        jobs = all_jobs()
        jobs[-1]["blocks"][0]["rows"][0]["result"] = "ERROR"
        self.assertEqual(v14.verdict(jobs, PRES)["outcome"], "NULL")
        jobs = copy.deepcopy(all_jobs())
        jobs[-1]["complete"] = False
        self.assertEqual(v14.verdict(jobs, PRES)["outcome"], "NULL")
        for w in ("r13", "r14"):
            jobs = [j for j in all_jobs() if j["r10_set"] != w]
            self.assertEqual(v14.verdict(jobs, PRES)["outcome"], "NULL", w)
        pres = dict(PRES)
        del pres["r14-ski/f"]
        self.assertEqual(v14.verdict(all_jobs(), pres)["outcome"], "NULL")
        jobs = all_jobs()  # an r13-harness job is not an r14 job
        jobs[0]["r13_harness"] = jobs[0].pop("r14_harness")
        self.assertEqual(v14.verdict(jobs, PRES)["outcome"], "NULL")

    def test_default_presence(self):
        self.assertEqual(v14.load_presence(), PRES)

    def test_scorer_unchanged(self):
        p = r14e.r14_suite()[0].probes[2]
        self.assertEqual(p.key, "e3x")
        self.assertEqual(r6.score_recency(p, p.current), "CORRECT")
        self.assertEqual(r6.score_recency(p, p.superseded[0]), "STALE")
        self.assertIs(r8.NEW_TARGET, r14e.NEW_TARGET)


if __name__ == "__main__":
    unittest.main()
