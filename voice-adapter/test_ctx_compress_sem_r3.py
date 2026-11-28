"""C1-ctx-sem-r3: no static reservation + SOFT budget rule (a superseding line trimmed to its value-bearing clause, in
the older line's slot, never longer than that slot + SLACK). Offline, no model (oracle / failing stand-ins).

Tests-first check: SEM3_UNDER_TEST=sem2 runs the behaviour tests (marked [behaviour]) against sem-r2's selection; they
must FAIL there and pass on sem-r3 (recorded in the PREREG).
"""
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
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_sem2 as sem2  # noqa: E402
import brief_compress_sem3 as sem3  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import ctx_compress_sem_r3_eval as e  # noqa: E402
import ctx_compress_sem_r3_verdict as v  # noqa: E402
import test_ctx_compress_r10 as t10  # noqa: E402
import test_ctx_compress_sem_r2 as t2  # noqa: E402

BUDGET = e.BUDGET
ALL_SETS = ("main",) + e.PROBE_SETS
UNDER = os.environ.get("SEM3_UNDER_TEST", "sem3")


def compress_info(text, budget, cache, judge=None):
    """The selection under test: sem-r3 (default) or sem-r2 (tests-first RED check)."""
    if UNDER == "sem2":
        out, info = sem2.compress_brief_sem2_info(text, budget, cache, judge)
        return out, {**info, "trimmed": [], "untrimmed": 0}
    return sem3.compress_brief_sem3_info(text, budget, cache, judge)


def briefs():
    for which in ALL_SETS:
        for s in r15e.SETS[which]():
            yield f"{which}/{s.name}", s, r2.chat_brief(s)


def blind_sets():
    yield "r15", r15e.BLIND, r15e.r15_suite()
    yield "v2", e.BLIND2, e.v2_suite()


def small_brief():
    lines = ["Brief header"]
    for u in ("My locker is Oak-1, thanks a lot for writing it down for me today.",
              "My bike in the rack is B-2, the blue one.",
              "I moved things around this week, my locker is now Oak-22, upstairs near the north window, "
              "next to the big plant that the staff water every Monday morning before the doors open."):
        lines += [f"User: {u}", "Assistant: Noted."]
    return "\n".join(lines)


class Trim(unittest.TestCase):
    def test_value_clause(self):
        self.assertEqual(sem3.value_clause("I switched to a family plan, my membership ID is now Alpha-108."),
                         "my membership ID is now Alpha-108.")
        self.assertEqual(sem3.value_clause("The access code is now DOOR-55."), "The access code is now DOOR-55.")
        self.assertEqual(sem3.value_clause("My parking spot is B12, code PARK-12."), "code PARK-12.")
        self.assertIsNone(sem3.value_clause("no value here"))

    def test_trim_levels_deterministic(self):
        ex = ["user: I changed my appointment, new reference is Crown-45. I am writing it down here as well.",
              "assistant: Code Crown-45, got it."]
        lv = sem3.trim_levels(ex)
        self.assertEqual([x[0] for x in lv], [1, 2, 3])
        self.assertEqual(lv[0][1], ["user: I changed my appointment, new reference is Crown-45.",
                                    "assistant: Code Crown-45, got it."])
        self.assertEqual(lv[1][1], ["user: I changed my appointment, new reference is Crown-45."])
        self.assertEqual(lv[2][1], ["user: new reference is Crown-45."])
        size = sem3.bc._size
        self.assertEqual(sem3.trim_exchange(ex, size([lv[0][1]]))[0], 1)
        self.assertEqual(sem3.trim_exchange(ex, size([lv[1][1]]) - sem3.SLACK)[0], 2)
        self.assertEqual(sem3.trim_exchange(ex, size([lv[2][1]]) - sem3.SLACK)[0], 3)
        self.assertEqual(sem3.trim_exchange(ex, 5), (None, None))
        self.assertEqual(sem3.trim_levels(["user: hello there", "assistant: hi"]), [])


class SoftBudget(unittest.TestCase):
    def _cache(self, text, budget):
        keys = {"Oak-1": "locker", "Oak-22": "locker", "B-2": "bike"}
        c = sem2.KeyCache(lambda s: next(k for val, k in keys.items() if re.search(val + r"\b", s)),
                          lambda o, n: "replace")
        t2.warm(text, c, budget)
        return c

    def test_superseding_line_takes_older_slot_trimmed(self):
        """[behaviour] the newer locker value replaces the older one IN ITS SLOT (position 1), trimmed, the bike
        static stays, and the budget holds; sem-r2 keeps the newer line at its own position untrimmed or drops it."""
        text = small_brief()
        lines = text.split("\n")
        budget = len("\n".join(lines[:5])) + 2
        c = self._cache(text, budget)
        out, info = compress_info(text, budget, c)
        self.assertLessEqual(len(out), budget)
        self.assertIn("B-2", out)
        self.assertNotIn("Oak-1,", out)
        self.assertIn("Oak-22", out)
        got = out.split("\n")
        self.assertTrue(got[1].startswith("User: ") and "Oak-22" in got[1], got)   # older line's position
        self.assertLess(got.index(got[1]), next(i for i, ln in enumerate(got) if "B-2" in ln))
        self.assertEqual(len(info["trimmed"]), 1)

    def test_trimmed_line_never_exceeds_slot_plus_slack(self):
        for name, s, brief in briefs():
            c = sem2.KeyCache(lambda st: "same thing", lambda o, n: "replace")   # worst case: everything replaces
            t2.warm(brief, c)
            out, info = sem3.compress_brief_sem3_info(brief, BUDGET, c)
            self.assertLessEqual(len(out), BUDGET, name)
            self.assertEqual(info["reserved"], 0, name)
            for _, _, _, chars, slot in info["trimmed"]:
                self.assertLessEqual(chars, slot + sem3.SLACK, name)

    def test_oracle_capacity_on_both_blind_sets(self):
        """[behaviour] perfect keys + judge: EVERY blind update current and never stale (r15 9/9, v2 17/17); every
        TWO_OLD / STATIC value r4 holds is kept. sem-r2 (reservation) reaches only 6/9 + 11/17."""
        for which, data, suite in blind_sets():
            for i, scn in enumerate(data["scenarios"]):
                brief = r2.chat_brief(suite[i])
                key1, judge = t2.oracle_for(scn)
                c = sem2.KeyCache(key1, judge)
                t2.warm(brief, c)
                out, _ = compress_info(brief, BUDGET, c)
                a = r4.compress_brief(brief, BUDGET)
                self.assertLessEqual(len(out), BUDGET)
                for p in scn["probes"]:
                    tag = (which, scn["name"], p["key"])
                    if p["cat"] in ("TWO_OLD", "STATIC") and e.has(p["current"], a):
                        self.assertTrue(e.has(p["current"], out), tag)
                    elif p["cat"].startswith("UPD"):
                        self.assertTrue(e.has(p["current"], out), tag)
                        for x in p["superseded"]:
                            self.assertFalse(e.has(x, out), tag)


class Fallback(unittest.TestCase):
    def test_no_cache_is_r4_byte_identical_everywhere(self):
        n = 0
        for name, _, brief in briefs():
            self.assertEqual(sem3.compress_brief_sem3(brief, BUDGET), r4.compress_brief(brief, BUDGET), name)
            n += 1
        self.assertGreaterEqual(n, 24)

    def test_failing_or_distinct_or_also_is_r4_byte_identical(self):
        def boom(*_):
            raise TimeoutError("timed out")
        cases = ((boom, boom), (lambda s: None, boom), (lambda s: "k " + sem2.stmt_hash(s)[:12], boom),
                 (lambda s: "same thing", lambda o, n: "also"), (lambda s: "same thing", lambda o, n: "maybe"))
        for key1, judge in cases:
            for name, _, brief in briefs():
                c = sem2.KeyCache(key1, judge)
                t2.warm(brief, c)
                out, info = sem3.compress_brief_sem3_info(brief, BUDGET, c, judge)
                self.assertEqual(out, r4.compress_brief(brief, BUDGET), name)
                self.assertEqual(info["replaced"], 0, name)

    def test_within_budget_and_degenerate(self):
        c = sem2.KeyCache(lambda s: 1 / 0, lambda o, n: 1 / 0)
        self.assertEqual(sem3.compress_brief_sem3("short", 100, c), "short")
        self.assertEqual(sem3.compress_brief_sem3("", 100, c), "")
        self.assertEqual(sem3.compress_brief_sem3("x" * 50, 0, c), "")
        no_speakers = "line\n" * 100
        self.assertEqual(sem3.compress_brief_sem3(no_speakers, 40, c), r4.compress_brief(no_speakers, 40))


class BlindV2(unittest.TestCase):
    def test_counts_and_names(self):
        cats = [p["cat"] for s in e.BLIND2["scenarios"] for p in s["probes"]]
        self.assertEqual({c: cats.count(c) for c in set(cats)},
                         {"UPD_SAME": 8, "UPD_RENAMED": 9, "TWO_OLD": 6, "TWO_NEW": 6, "STATIC": 21})
        self.assertEqual(e.V2_NAMES, ["v2-dentist", "v2-move-in", "v2-gym", "v2-pickup", "v2-internet",
                                      "v2-book-club"])
        for g in e.V2_GROUPS:
            self.assertLessEqual(max(len(g[0]), len(g[1])), 5)

    def test_suite_carries_every_fact_verbatim_and_old_facts_fit(self):
        for i, s in enumerate(e.v2_suite()):
            sc = e.BLIND2["scenarios"][i]
            chat = "\n".join(t.text for t in s.chat)
            for f in sc["old"] + sc["new"]:
                self.assertIn(f["text"], chat, (s.name, f["text"]))
                self.assertTrue(e.has(f["value"], f["text"]), (s.name, f["value"]))
            brief = r2.chat_brief(s)
            self.assertGreater(len(brief), BUDGET, s.name)
            a = r4.compress_brief(brief, BUDGET)
            for f in sc["old"]:   # r4 keeps every older fact (the r15 harness property)
                self.assertTrue(e.has(f["value"], a), (s.name, f["value"]))

    def test_presence_whole_code(self):
        self.assertTrue(e.has("GENRE-2022", "x GENRE-2022."))
        self.assertFalse(e.has("GENRE-202", "x GENRE-2022."))
        self.assertFalse(e.has("Time-300", "Time-300B is"))
        pres = e.presence()
        self.assertTrue(any(k.startswith("v2-") for k in pres))
        self.assertTrue(any(k.startswith("r15-") for k in pres))


class Harness(unittest.TestCase):
    def test_frozen_modules_untouched(self):
        sha = {"brief_compress.py": "5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976",
               "brief_compress_sem2.py": "129641dddacf1a4aad10077bf41304f9b3c8865ac2fc928b8e560b05e9f59101",
               "bench/ctx_compress_r15_blind.json": "aeb7790730ea628a699a877ba18ab46ead81fc5fb2b5fdd7cb863d7a26b8208a"}
        for f, h in sha.items():
            self.assertEqual(hashlib.sha256((HERE / f).read_bytes()).hexdigest(), h, f)

    def test_arms_outside_a_block(self):
        for name, _, brief in briefs():
            self.assertEqual(e.COMPRESS["A"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["B"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["O"](brief), brief)

    def test_block_wrapper_annotates_rows_and_resets(self):
        s = e.v2_suite()[0]
        sc = e.BLIND2["scenarios"][0]
        key1, judge = t2.oracle_for(sc)
        saved = (e.sem2.llm_key1, e.sem2.llm_judge, e.lanespeed.ask)
        e.sem2.llm_key1 = lambda url, log=None, timeout=0: key1
        e.sem2.llm_judge = lambda url, log=None, timeout=0: judge
        e.lanespeed.ask = lambda *a, **k: ("ok", 0.1, 0.1, 0.1, {})

        def fake_block(scenarios, arm, t0, dl):
            return [{"scenario": x.name, "key": p.key, "result": "RECALL"} for x in scenarios for p in x.probes], False
        try:
            rows, stopped = e.make_run_block(fake_block)([s], "B", 0, 1)
            rows_a, _ = e.make_run_block(fake_block)([s], "A", 0, 1)
        finally:
            e.sem2.llm_key1, e.sem2.llm_judge, e.lanespeed.ask = saved
        self.assertFalse(stopped)
        self.assertIsNone(e._STATE["arm"])
        self.assertTrue(all("sem" in r and "in_brief" in r and "in_brief_ablation" in r for r in rows))
        self.assertTrue(all("sem" not in r for r in rows_a))
        by = {r["key"]: r for r in rows}
        for p in sc["probes"]:
            if p["cat"] in ("TWO_OLD", "STATIC") or p["cat"].startswith("UPD"):
                self.assertTrue(by[p["key"]]["in_brief"], p["key"])
        self.assertEqual(set(rows[0]["in_brief_ablation"]), {"nojudge", "sem2"})
        self.assertGreaterEqual(len(rows[0]["sem"]["trimmed"]), 3)


def mock_jobs(result=None, sem_wall=0.001):
    """Every scored set, layout of the workflow; B rows carry sem / in_brief / in_brief_ablation."""
    result = result or (lambda which, lab, s, p: "CORRECT" if p.superseded else "RECALL")
    groups = {"r14": ((0, 1, 2, 3, 4, 5, 6),), "r13": ((0, 1, 2, 3, 4),), "r12": ((0, 1, 2, 3, 4, 5, 6),),
              "recency": ((0, 1, 2),), "r8": ((0, 1, 2, 5),)}
    sem = {k: None for k in v.SEM_FIELDS}
    sem.update(brief_wall_s=sem_wall, fallback=None, cache_errors=[], arrival_walls=[1.0], trimmed=[], untrimmed=0,
               candidates=10, collisions=2, replaced=1, reserved=0, key_misses=0, judge_misses=0, cold_total_s=1.0,
               judge_in_path_s=0.5, keys=[])
    jobs = []
    for which in e.PROBE_SETS:
        for i, s in enumerate(r15e.SETS[which]()):
            gs = (r15e.R15_GROUPS[i] if which == "r15" else e.V2_GROUPS[i] if which == "v2" else groups[which])
            for idx in gs:
                bl = []
                for lab in ("B1", "A1", "B2", "A2"):
                    rows = []
                    for k in idx:
                        p = s.probes[k]
                        r = {"scenario": s.name, "key": p.key, "first_sentence_s": 1.0,
                             "result": result(which, lab, s, p)}
                        if lab[0] == "B":
                            r.update(sem=copy.deepcopy(sem), in_brief=True,
                                     in_brief_ablation={"nojudge": True, "sem2": True})
                        rows.append(r)
                    bl.append({"label": lab, "arm": lab[0], "rows": rows})
                jobs.append({"r10_set": which, "sem_harness": v.HARNESS, "scenarios": [s.name], "complete": True,
                             "blocks": bl})
    for j in t10.main_jobs():
        j["sem_harness"] = v.HARNESS
        jobs.append(j)
    return jobs


class Verdict(unittest.TestCase):
    PRES = None

    @classmethod
    def setUpClass(cls):
        cls.PRES = e.presence()

    def test_mock_all_correct_is_not_null_and_pools(self):
        out = v.verdict(mock_jobs(), self.PRES)
        self.assertEqual(out["null_reasons"], [])
        self.assertEqual(out["pooled"]["update_correct"]["B1"], 26)
        self.assertEqual(out["pooled"]["static_recall"]["A1"], 32)
        self.assertEqual(out["per_blind_set"]["v2"]["update_correct"]["B2"], 17)
        self.assertTrue(out["checks"]["G1s_blind_static_recall"])

    def test_g1_pooled_loss_and_g8a(self):
        def res(which, lab, s, p):
            if which == "v2" and lab[0] == "B" and p.key == "p_old" and s.name == "v2-gym":
                return "ABSTAIN"
            if which in ("r15", "v2") and lab[0] == "A" and p.superseded:
                return "STALE"
            return "CORRECT" if p.superseded else "RECALL"
        out = v.verdict(mock_jobs(res, sem_wall=4.0), self.PRES)
        self.assertEqual(out["null_reasons"], [])
        self.assertFalse(out["checks"]["G1_blind_two_true_loss"])
        self.assertTrue(out["checks"]["G1e_blind_update_fixed"])
        self.assertFalse(out["checks"]["G8a_brief_latency"])
        self.assertEqual(out["outcome"], "REVERT")

    def test_missing_v2_job_is_null(self):
        jobs = [j for j in mock_jobs() if j["scenarios"] != ["v2-gym"]]
        out = v.verdict(jobs, self.PRES)
        self.assertEqual(out["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()
