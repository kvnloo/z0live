"""C1-ctx-compress-r15: blind held-out test of the r14 rule (harness-only round). The blind set (written by a free
hosted LLM that never saw the compressor, the rule or an earlier set), its conversion into the harness format,
deterministic pre-check / dry prediction / presence, and the r15 verdict on mock jobs. No model."""
import copy
import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress as bc  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_r13 as r13  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r12_eval as r12e  # noqa: E402
import ctx_compress_r13_eval as r13e  # noqa: E402
import ctx_compress_r14_eval as r14e  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import ctx_compress_r15_verdict as v15  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402

R14_SHA256 = "5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976"  # brief_compress.py at 1c078bf (r14)
BLIND = json.loads((HERE / "bench" / "ctx_compress_r15_blind.json").read_text())
# local only: the raw blind texts are kept outside the public repo; set R15_BLIND_RAW to their directory
RAW = os.environ.get("R15_BLIND_RAW")

# ---------------------------------------------------------------------------------------------------------------
# Offline dry prediction for the r15 set (frozen in the r15 PREREG, written before scoring): what each arm's context
# holds. CURRENT = planted value in context, STALE = only the superseded value, BOTH = both, ABSENT = neither.
_UPD = ("u1", "u2", "u3")
_STATIC = {"r15-vet": ("s1", "s2", "s3"), "r15-car": ("s1", "s2", "s3", "s4", "s5"),
           "r15-pottery": ("s1", "s2", "s3")}
_NEW = {"r15-vet": ("p_new",), "r15-car": (), "r15-pottery": ("p_new",)}


def _dry(arm, name):
    upd = {"A": "STALE", "B": "STALE", "R": "STALE", "O": "BOTH"}[arm]
    out = {**{k: upd for k in _UPD}, "p_old": "CURRENT", **{k: "CURRENT" for k in _STATIC[name]},
           **{k: ("CURRENT" if arm == "O" else "ABSENT") for k in _NEW[name]}}
    return out


DRY = {arm: {n: _dry(arm, n) for n in r15e.R15_NAMES} for arm in "ABRO"}
PRES = r15e.presence()
LAYOUT = {"r15": ("B,A,B,A,O,R", None),
          "r14": ("B,A,B,A", ((0, 1, 2, 3, 4, 5, 6),)),
          "r13": ("B,A,B,A", ((0, 1, 2, 3, 4),)),
          "r12": ("B,A,B,A", ((0, 1, 2, 3, 4, 5, 6),)),
          "recency": ("B,A,B,A", ((0, 1, 2),)),
          "r8": ("B,A,B,A", ((0, 1, 2, 5),))}
_DRYROWS = {w: {row["scenario"]: row for row in r15e.dry(r15e.SETS[w]())} for w in LAYOUT}


def key(text, mod=bc):
    return mod._attribute([f"user: {text}", "assistant: ok"])


def predicted(which, lab, name, k, missing="ABSTAIN"):
    want = _DRYROWS[which][name][lab[0]]["items"][k]
    p = next(q for q in r15e.SETS[which]() if q.name == name).probes
    sup = bool(next(q for q in p if q.key == k).superseded)
    return {"STALE": "STALE", "BOTH": "BOTH", "ABSENT": missing}.get(want, "CORRECT" if sup else "RECALL")


def set_jobs(which, results=None, missing="ABSTAIN"):
    results = results or {}
    blocks, groups = LAYOUT[which]
    jobs = []
    for i, s in enumerate(r15e.SETS[which]()):
        for idx in (r15e.R15_GROUPS[i] if which == "r15" else groups):
            labs, seen = [], {}
            for a in blocks.split(","):
                seen[a] = seen.get(a, 0) + 1
                labs.append(f"{a}{seen[a]}")
            bl = [{"label": lab, "arm": lab[0], "rows": [
                {"scenario": s.name, "key": s.probes[k].key, "first_sentence_s": 1.0,
                 "result": results.get((lab, s.name, s.probes[k].key),
                                       predicted(which, lab, s.name, s.probes[k].key, missing))}
                for k in idx]} for lab in labs]
            jobs.append({"r10_set": which, "r15_harness": v15.HARNESS, "scenarios": [s.name], "complete": True,
                         "blocks": bl})
    return jobs


def all_jobs(results=None, missing="ABSTAIN"):
    main = t10.main_jobs()
    for j in main:
        j["r15_harness"] = v15.HARNESS
    return main + [j for w in LAYOUT for j in set_jobs(w, results, missing)]


class BlindSet(unittest.TestCase):
    def test_counts_and_categories(self):
        cats = [p["cat"] for s in BLIND["scenarios"] for p in s["probes"]]
        self.assertEqual({c: cats.count(c) for c in set(cats)},
                         {"UPD_SAME": 5, "UPD_RENAMED": 4, "TWO_OLD": 3, "TWO_NEW": 2, "STATIC": 11})
        self.assertEqual([s["name"] for s in BLIND["scenarios"]], r15e.R15_NAMES)
        self.assertEqual(r15e.R15_NAMES, ["r15-vet", "r15-car", "r15-pottery"])

    @unittest.skipUnless(RAW, "raw blind texts are local only (R15_BLIND_RAW)")
    def test_verbatim_from_raw(self):
        """Every fact sentence is a verbatim substring of a User line of the raw blind text (no edits)."""
        for s in BLIND["scenarios"]:
            raw = (Path(RAW) / s["raw"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), s["raw_sha256"], s["name"])
            user = [ln.strip() for ln in raw.decode().split("KEY:")[0].splitlines() if ln.startswith("User:")]
            for f in s["old"] + s["new"]:
                self.assertTrue(any(f["text"] in ln for ln in user), (s["name"], f["text"]))
            texts = [f["text"] for f in s["old"] + s["new"]]
            order = [next(i for i, ln in enumerate(user) if t in ln) for t in texts]
            self.assertEqual(len(set(order)), len(order), s["name"])
            no = len(s["old"])  # each block keeps the blind chat's order
            self.assertEqual(order[:no], sorted(order[:no]), s["name"])
            self.assertEqual(order[no:], sorted(order[no:]), s["name"])
            for p in s["probes"]:  # every update / pair partner comes after the fact it updates / pairs with
                if p["cat"].startswith("UPD"):
                    o = texts.index(next(f["text"] for f in s["old"] if f["value"] == p["superseded"][0]))
                    n = texts.index(next(f["text"] for f in s["new"] if f["value"] == p["current"]))
                    self.assertLess(order[o], order[n], (s["name"], p["key"]))

    def test_gold_from_blind_text(self):
        for s in BLIND["scenarios"]:
            old = {f["value"]: f["text"] for f in s["old"]}
            new = {f["value"]: f["text"] for f in s["new"]}
            for f in s["old"] + s["new"]:
                self.assertIn(f["value"], f["text"])
            for p in s["probes"]:
                if p["cat"].startswith("UPD"):
                    self.assertIn(p["current"], new, (s["name"], p["key"]))
                    self.assertEqual(len(p["superseded"]), 1)
                    self.assertIn(p["superseded"][0], old, (s["name"], p["key"]))
                    self.assertNotIn(p["superseded"][0], new[p["current"]])  # the update does not restate it
                elif p["cat"] == "TWO_NEW":
                    self.assertIn(p["current"], new)
                    self.assertEqual(p["superseded"], [])
                else:
                    self.assertIn(p["current"], old, (s["name"], p["key"]))
                    self.assertEqual(p["superseded"], [])

    def test_codes_held_out(self):
        vals = [f["value"] for s in BLIND["scenarios"] for f in s["old"] + s["new"]]
        self.assertEqual(len(vals), len(set(vals)))
        stable = " ".join(t.text for s in r10e.SETS["main"]() for t in s.chat)
        earlier = " ".join(t.text for w in ("recency", "r8", "r10", "r12", "r13", "r14") for s in r14e.SETS[w]()
                           for t in s.chat)
        for v in vals:
            self.assertIsNone(re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", stable + " " + earlier), v)
        for s in r15e.r15_suite():  # a value appears in no other turn of its scenario, except the pair sentence
            for p in s.probes:
                hits = [t.text for t in s.chat if t.role == "user" and p.current in t.text]
                self.assertEqual(len(hits), 2 if p.current == "FLT-06" else 1, (s.name, p.key))

    def test_harness_text_has_no_rule_words(self):
        """The padding and acknowledgement the harness adds carry no slot, cue or digit (they cannot change a key)."""
        pad = " ".join(r8.PAD) + r15e.NEW_NOTE
        self.assertIsNone(bc.CUE.search(pad))
        self.assertNotIn("my", bc.WORD.findall(pad.lower()))
        self.assertIsNone(bc.SLOT.search(pad))
        self.assertFalse(any(c.isdigit() for c in pad))
        for s in r15e.r15_suite():
            for t in s.chat:
                if t.role == "user":
                    f = next((x["text"] for sc in BLIND["scenarios"] for x in sc["old"] + sc["new"]
                              if t.text.startswith(x["text"])), None)
                    if f is not None:
                        self.assertEqual(key(t.text), key(f), t.text)
                        self.assertEqual(bc._cue([f"user: {t.text}"]), bc._cue([f"user: {f}"]), t.text)


class SurfaceForm(unittest.TestCase):
    """How r14 reads each blind statement (measured offline, recorded before scoring)."""

    def test_r14_supersedes_nothing(self):
        for i, s in enumerate(BLIND["scenarios"]):
            st = r15e.statements(i)
            for p in s["probes"]:
                if not p["cat"].startswith("UPD"):
                    continue
                o, n = st[p["superseded"][0]], st[p["current"]]
                same = key(o) is not None and key(o) == key(n) and bc._cue([f"user: {n}"])
                self.assertFalse(same, (s["name"], p["key"], key(o), key(n)))

    def test_slot_annotation(self):
        """The 'slot' note of every update matches the measured SLOT match of its two statements."""
        want = {"both (cue word inside the new subject)": (True, True), "old only": (True, False),
                "none": (False, False)}
        for i, s in enumerate(BLIND["scenarios"]):
            st = r15e.statements(i)
            for p in s["probes"]:
                if p["cat"].startswith("UPD"):
                    o, n = st[p["superseded"][0]], st[p["current"]]
                    self.assertEqual((key(o) is not None, key(n) is not None), want[p["slot"]], (s["name"], p["key"]))

    def test_r14_keys_pinned(self):
        self.assertEqual(r15e.keys(r15e.statements(1))["BOOK-05"]["r14"][0], "updated car rental booking reference")
        self.assertEqual(r15e.keys(r15e.statements(1))["BOOK-01"]["r14"][0], "car rental booking reference")
        self.assertIsNone(r15e.keys(r15e.statements(0))["PUP-205"]["r14"])
        self.assertIsNone(r15e.keys(r15e.statements(2))["ClayRef-02"]["r14"])


class Production(unittest.TestCase):
    def test_brief_compress_is_r14(self):
        self.assertEqual(hashlib.sha256((HERE / "brief_compress.py").read_bytes()).hexdigest(), R14_SHA256)

    def test_arms(self):
        b = r2.chat_brief(r15e.r15_suite()[0])
        self.assertEqual(r15e.COMPRESS["A"](b), r4.compress_brief(b, 2810))
        self.assertEqual(r15e.COMPRESS["B"](b), bc.compress_brief(b, 2810))
        self.assertEqual(r15e.COMPRESS["R"](b), r13.compress_brief(b, 2810))
        self.assertEqual(r15e.COMPRESS["O"](b), b)
        self.assertEqual(set(r15e.COMPRESS), set("ABRO"))


class Loader(unittest.TestCase):
    def test_suite_shape(self):
        suite = r15e.r15_suite()
        self.assertEqual([s.name for s in suite], r15e.R15_NAMES)
        for sc, s in zip(BLIND["scenarios"], suite):
            self.assertEqual(tuple(p.key for p in s.probes), tuple(p["key"] for p in sc["probes"]))
            self.assertEqual(len(s.chat), 2 * (len(sc["old"]) + r8.NOISE_PAIRS + len(sc["new"])))
            for p, q in zip(s.probes, sc["probes"]):
                self.assertEqual((p.current, p.superseded), (q["current"], tuple(q["superseded"])))
                self.assertEqual(p.ask, q["ask"] + " Please give just the code.")

    def test_sizes_old_lt_noise_lt_new(self):
        for sc, s in zip(BLIND["scenarios"], r15e.r15_suite()):
            _, exs, _ = bc._split(r2.chat_brief(s).split("\n"))
            sizes = [bc._size([x]) for x in exs]
            no, nn = len(sc["old"]), r8.NOISE_PAIRS
            old, noise, new = sizes[:no], sizes[no:no + nn], sizes[no + nn:]
            self.assertEqual(len(new), len(sc["new"]))
            self.assertLessEqual(max(old), r15e.OLD_TARGET, s.name)
            self.assertLess(max(old), min(noise), s.name)
            self.assertLess(max(noise), min(new), s.name)
            self.assertLessEqual(max(new), r15e.NEW_TARGET)

    def test_groups_cover_probes(self):
        for i, s in enumerate(r15e.r15_suite()):
            flat = [k for g in r15e.R15_GROUPS[i] for k in g]
            self.assertEqual(flat, list(range(len(s.probes))))
            self.assertTrue(all(len(g) <= 5 for g in r15e.R15_GROUPS[i]))  # <= 30 requests per job (6 blocks)
        wf = (HERE.parent / ".github" / "workflows" / "ctx-compress-r15.yml").read_text()
        g = lambda t: ",".join(map(str, t))  # noqa: E731
        self.assertIn(f'hx) if [ "$SC" = 1 ]; then P={g(r15e.R15_GROUPS[1][0])}; else P={g(r15e.R15_GROUPS[0][0])}; fi',
                      wf)
        self.assertIn(f'hy) if [ "$SC" = 1 ]; then P={g(r15e.R15_GROUPS[1][1])}; else P={g(r15e.R15_GROUPS[0][1])}; fi',
                      wf)
        self.assertEqual(r15e.R15_GROUPS[0], r15e.R15_GROUPS[2])
        self.assertIn("test_ctx_compress_r15)", wf)

    def test_frozen_counts(self):
        self.assertEqual(len(PRES), 25 + 27 + 30 + 33 + 21 + 9)  # r15 r14 r13 r12 r8 recency
        for w in ("r14", "r13", "r12", "recency", "r8"):  # every gated key is scored, and only the declared ones
            for s in r15e.SETS[w]():
                keys = {s.probes[k].key for g in LAYOUT[w][1] for k in g}
                want = {k for c, ks in v15.SETS[w]["cats"].items() for k in ks}
                self.assertEqual(keys, want, (w, s.name))
        self.assertEqual(v15.r15_cats(), {c: tuple(sorted(f"{s['name']}/{p['key']}" for s in BLIND["scenarios"]
                                                          for p in s["probes"] if p["cat"] == c))
                                          for c in ("UPD_SAME", "UPD_RENAMED", "TWO_OLD", "TWO_NEW", "STATIC")})


class Prediction(unittest.TestCase):
    def test_no_leak(self):
        before, sets = r10e.COMPRESS, dict(r10e.SETS)
        r15e.dry(r15e.r15_suite())
        r15e.presence()
        self.assertIs(r10e.COMPRESS, before)
        self.assertEqual(r10e.SETS, sets)

    def test_dry_prediction(self):
        for row in r15e.dry(r15e.r15_suite()):
            for arm in "ABRO":
                self.assertEqual(row[arm]["items"], DRY[arm][row["scenario"]], (row["scenario"], arm))
                self.assertLessEqual(row[arm]["brief_chars"], 2810 if arm != "O" else 10 ** 6)

    def test_b_equals_a_on_r15(self):
        """Measured offline: r14 groups no blind update, so B's brief is r4's byte for byte on every r15 scenario."""
        for s in r15e.r15_suite():
            b = r2.chat_brief(s)
            self.assertEqual(bc.compress_brief(b, 2810), r4.compress_brief(b, 2810), s.name)

    def test_precheck_passes(self):
        pc = r15e.precheck()
        self.assertEqual(pc["stop"], [])
        self.assertEqual(len(pc["briefs"]), 3 * 7)
        for name, row in pc["briefs"].items():
            self.assertLessEqual(max(row["chars"].values()), 2810, name)
            if name.startswith("main/"):
                self.assertTrue(row["r14_eq_r4"], name)

    def test_presence_map(self):
        for n in r15e.R15_NAMES:
            for k in ("p_old",) + _STATIC[n]:
                self.assertEqual(PRES[f"{n}/{k}"], {"A": True, "B": True}, (n, k))
            for k in _UPD + _NEW[n]:
                self.assertEqual(PRES[f"{n}/{k}"], {"A": False, "B": False}, (n, k))


class Verdict(unittest.TestCase):
    def test_dry_prediction_keeps(self):
        v = v15.verdict(all_jobs(), PRES)
        self.assertEqual((v["outcome"], v["failed"]), ("KEEP", []), v["null_reasons"])
        self.assertEqual(v["r15_two_true_loss"], {"A1": 0, "B1": 0, "A2": 0, "B2": 0, "O1": 0, "R1": 0})
        self.assertEqual(v["r15_update_stale"], {"A1": 9, "B1": 9, "A2": 9, "B2": 9, "O1": 0, "R1": 9})
        self.assertEqual(v["r15_update_stale_split"]["UPD_SAME"]["B1"], 5)
        self.assertEqual(v["r15_update_stale_split"]["UPD_RENAMED"]["B1"], 4)
        self.assertEqual(v["r15_static_recall"]["B1"], 11)
        for w in v15.DEV_SETS:
            self.assertEqual((v["stale"][w]["A1"], v["stale"][w]["B1"]), (12 if w == "r14" else 6, 0), w)
            self.assertEqual(v["unique_recall"][w]["B1"], 3, w)
        self.assertEqual(v["presence_counts"]["scored_probes"], 25 + 21 + 15 + 21 + 9 + 12)
        self.assertEqual(v["presence_counts"]["absent_in_both"], 9 + 2)
        self.assertEqual(v["band"], 0)

    def test_two_true_loss_reverts(self):
        res = {(lab, n, "p_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r15e.R15_NAMES}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["failed"], ["G1_r15_two_true_loss"])

    def test_update_stale_reverts(self):
        res = {(lab, n, k): "CORRECT" for lab in ("A1", "A2") for n in r15e.R15_NAMES for k in _UPD}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["failed"], ["G1e_r15_update_stale"])
        res = {(lab, n, k): "CORRECT" for lab in ("B1", "B2") for n in r15e.R15_NAMES for k in _UPD}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["outcome"], "KEEP")

    def test_static_reverts(self):
        res = {(lab, "r15-car", k): "ABSTAIN" for lab in ("B1", "B2") for k in _STATIC["r15-car"]}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["failed"], ["G1s_r15_static_recall"])

    def test_dev_gates(self):
        res = {(lab, n, "a3x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r14e.R14_NAMES}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["failed"], ["G1b_r14_a3_loss"])
        res = {(lab, n, "a2x_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r13e.R13_NAMES}
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["failed"], ["G1c_dev_fm_loss"])
        for w, ks, names in (("r14", ("e3x", "e3y", "e1", "e2"), r14e.R14_NAMES), ("r13", ("e1", "e2"), r13e.R13_NAMES),
                             ("recency", ("bench", "badge"), r6.RECENCY_NAMES)):
            res = {(lab, n, k): "STALE" for lab in ("B1", "B2") for n in names for k in ks}
            v = v15.verdict(all_jobs(res), PRES)
            self.assertIn("G2_stale_fixed", v["failed"], w)
            self.assertFalse(v["stale_ok"][w])
        res = {(lab, n, "f"): "ABSTAIN" for lab in ("B1", "B2") for n in r12e.R12_NAMES}
        self.assertIn("G3_unique_kept", v15.verdict(all_jobs(res), PRES)["failed"])

    def test_report_only(self):
        res = {(lab, n, "p_new"): "ABSTAIN" for lab in ("B1", "B2") for n in ("r15-vet", "r15-pottery")}
        res.update({(lab, n, k): "INVENTED" for lab in ("B1", "B2", "A1", "A2") for n in r15e.R15_NAMES for k in _UPD})
        self.assertEqual(v15.verdict(all_jobs(res), PRES)["outcome"], "KEEP")

    def test_invent_mode(self):
        v = v15.verdict(all_jobs(missing="INVENTED"), PRES)
        self.assertEqual(v["outcome"], "KEEP")
        self.assertEqual(v["invented_present"]["A1"], v["invented_present"]["B1"])
        res = {(lab, n, "s1"): "INVENTED" for lab in ("B1", "B2") for n in r15e.R15_NAMES}
        self.assertIn("G6_no_new_invented_present", v15.verdict(all_jobs(res), PRES)["failed"])

    def test_band_widens(self):
        res = {("A2", n, k): "ABSTAIN" for n in r12e.R12_NAMES for k in ("b_old", "c_old")}
        res.update({(lab, n, "p_old"): "ABSTAIN" for lab in ("B1", "B2") for n in r15e.R15_NAMES})
        v = v15.verdict(all_jobs(res), PRES)
        self.assertEqual(v["band"], 6)
        self.assertNotIn("G1_r15_two_true_loss", v["failed"])
        self.assertIn("G2_stale_fixed", v["failed"])  # 0 < 6 - 6 is false: a wide band also blocks KEEP

    def test_latency_gate(self):
        jobs = all_jobs()
        for j in jobs:
            for b in j["blocks"]:
                if j["r10_set"] == "main" and b["label"] in ("B1", "B2"):
                    for r in b["rows"]:
                        r["first_sentence_s"] = (r.get("first_sentence_s") or 0) * 2 + 100
        self.assertIn("G7_latency", v15.verdict(jobs, PRES)["failed"])

    def test_null(self):
        jobs = all_jobs()
        del jobs[-1]["r15_harness"]
        self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL")
        self.assertEqual(v15.verdict([], PRES)["outcome"], "NULL")
        self.assertEqual(v15.verdict(all_jobs()[:-1], PRES)["outcome"], "NULL")
        jobs = all_jobs()
        jobs[-1]["blocks"][0]["rows"][0]["result"] = "ERROR"
        self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL")
        jobs = copy.deepcopy(all_jobs())
        jobs[-1]["complete"] = False
        self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL")
        for w in ("r13", "r14", "r15"):
            jobs = [j for j in all_jobs() if j["r10_set"] != w]
            self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL", w)
        jobs = [j for j in all_jobs() if not (j["r10_set"] == "r15" and j["blocks"][0]["rows"][0]["key"] == "s1")]
        self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL")  # a missing r15 half job
        pres = dict(PRES)
        del pres["r15-car/s1"]
        self.assertEqual(v15.verdict(all_jobs(), pres)["outcome"], "NULL")
        jobs = all_jobs()  # an r14-harness job is not an r15 job
        jobs[0]["r14_harness"] = jobs[0].pop("r15_harness")
        self.assertEqual(v15.verdict(jobs, PRES)["outcome"], "NULL")

    def test_default_presence(self):
        self.assertEqual(v15.load_presence(), PRES)

    def test_scorer_unchanged(self):
        p = r15e.r15_suite()[0].probes[0]
        self.assertEqual(p.key, "u1")
        self.assertEqual(r6.score_recency(p, p.current), "CORRECT")
        self.assertEqual(r6.score_recency(p, p.superseded[0]), "STALE")
        self.assertIs(r8.NEW_TARGET, r15e.NEW_TARGET)


if __name__ == "__main__":
    unittest.main()
