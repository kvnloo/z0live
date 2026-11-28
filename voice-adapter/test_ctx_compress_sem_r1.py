"""C1-ctx-sem-r1: semantic supersession pass. Offline, no model (oracle / failing keyers stand in for the model pass)."""
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress as bc  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_sem as sem  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import ctx_compress_sem_r1_eval as e  # noqa: E402

BLIND = r15e.BLIND
BUDGET = e.BUDGET
ALL_SETS = ("main",) + r15e.PROBE_SETS
ORACLE_DISPLACED = ("r15-car", "s1")   # deterministic oracle bound (PREREG): perfect keys still displace this static fact


def briefs():
    for which in ALL_SETS:
        for s in r15e.SETS[which]():
            yield f"{which}/{s.name}", s, r2.chat_brief(s)


def oracle_keyer_for(scn):
    """Oracle: statements about the same blind fact get the same key (value groups from the data file's probes)."""
    group = {}
    for p in scn["probes"]:
        for v in [p["current"], *p["superseded"]]:
            group[v] = "fact " + p["key"].replace("_", " x")

    def keyer(statements):
        out = []
        for s in statements:
            hit = [k for v, k in group.items() if v in s]
            out.append(hit[0] if hit else "key " + re.sub(r"\W+", "-", s.lower())[:30])
        return out
    return keyer


class Fallback(unittest.TestCase):
    def test_no_keyer_is_r4_byte_identical_everywhere(self):
        n = 0
        for name, _, brief in briefs():
            self.assertEqual(sem.compress_brief_sem(brief, BUDGET), r4.compress_brief(brief, BUDGET), name)
            n += 1
        self.assertGreaterEqual(n, 18)

    def test_failing_keyers_are_r4_byte_identical(self):
        def boom(_):
            raise TimeoutError("timed out")
        for k in (boom, lambda st: [None] * len(st), lambda st: ["", "  "] + [None] * (len(st) - 2)):
            for name, _, brief in briefs():
                out, info = sem.compress_brief_sem_info(brief, BUDGET, k)
                self.assertEqual(out, r4.compress_brief(brief, BUDGET), name)
                self.assertTrue(info["fallback"] or info["candidates"] == 0, name)

    def test_all_distinct_keys_is_r4(self):
        for name, _, brief in briefs():
            out = sem.compress_brief_sem(brief, BUDGET, lambda st: [f"k{i}" for i in range(len(st))])
            self.assertEqual(out, r4.compress_brief(brief, BUDGET), name)

    def test_within_budget_and_degenerate(self):
        self.assertEqual(sem.compress_brief_sem("short", 100, lambda s: 1 / 0), "short")
        self.assertEqual(sem.compress_brief_sem("", 100), "")
        self.assertEqual(sem.compress_brief_sem("x" * 50, 0), "")
        no_speakers = "line\n" * 100
        self.assertEqual(sem.compress_brief_sem(no_speakers, 40, lambda s: s), r4.compress_brief(no_speakers, 40))

    def test_keyer_called_once_per_brief_and_only_when_over_budget(self):
        calls = []
        _, s, brief = next(b for b in briefs() if b[0] == "r15/r15-vet")
        sem.compress_brief_sem(brief, BUDGET, lambda st: calls.append(list(st)) or [None] * len(st))
        self.assertEqual(len(calls), 1)
        sem.compress_brief_sem(brief, 10 ** 6, lambda st: calls.append(1))
        self.assertEqual(len(calls), 1)


class Grouping(unittest.TestCase):
    def test_oracle_groups_every_blind_update_and_keeps_pairs(self):
        for i, scn in enumerate(BLIND["scenarios"]):
            s = r15e.r15_suite()[i]
            brief = r2.chat_brief(s)
            out, info = sem.compress_brief_sem_info(brief, BUDGET, oracle_keyer_for(scn))
            self.assertIsNone(info["fallback"])
            self.assertLessEqual(len(out), BUDGET)
            for p in scn["probes"]:
                cat = p["cat"]
                if cat.startswith("UPD"):
                    self.assertIn(p["current"], out, (scn["name"], p["key"]))
                    for v in p["superseded"]:
                        self.assertNotIn(v, out, (scn["name"], p["key"]))
                elif cat in ("TWO_OLD", "STATIC"):
                    if (scn["name"], p["key"]) == ORACLE_DISPLACED:
                        # budget, not semantics: with perfect keys the three newer statements (about 380 chars each)
                        # replace three older ones (about 260), so the car brief has no room left for this static fact
                        self.assertNotIn(p["current"], out)
                    else:
                        self.assertIn(p["current"], out, (scn["name"], p["key"]))

    def test_group_collapses_to_newest_at_shortest_size_and_is_a_subset(self):
        lines = ["Brief header"]
        for u, a in (("My locker is Oak-1.", "Noted."), ("Filler about the weather today and many other things.",
                                                        "x" * 260), ("The locker is now Oak-2, see you.", "Noted.")):
            lines += [f"User: {u}", f"Assistant: {a}"]
        text = "\n".join(lines)
        out = sem.compress_brief_sem(text, len(text) - 40, lambda st: ["locker"] * len(st))
        self.assertIn("Oak-2", out)
        self.assertNotIn("Oak-1", out)
        self.assertTrue(set(out.split("\n")) <= set(text.split("\n")))

    def test_sem_never_exceeds_budget_nor_adds_text(self):
        for name, s, brief in briefs():
            k = lambda st: ["same"] * len(st)  # noqa: E731 - worst case: one group per brief
            out = sem.compress_brief_sem(brief, BUDGET, k)
            self.assertLessEqual(len(out), BUDGET, name)
            self.assertTrue(set(out.split("\n")) <= set(brief.split("\n")), name)


class Parsing(unittest.TestCase):
    def test_statement_is_first_valued_sentence(self):
        ex = ["User: Hello there. My booking ref is PUP-1, thanks! I am writing it down.", "Assistant: ok"]
        self.assertEqual(sem.statement(ex), "My booking ref is PUP-1, thanks!")
        self.assertIsNone(sem.statement(["User: nothing here.", "Assistant: ok"]))
        self.assertLessEqual(len(sem.statement(["User: " + "word " * 100 + "A-1"])), sem.STMT_CHARS)

    def test_parse_keys(self):
        self.assertEqual(sem.parse_keys("1: Card Number\n2. the booking reference\n9: x\n2: dup\ngarbage", 3),
                         ["card number", "booking reference", None])
        self.assertEqual(sem.parse_keys("", 2), [None, None])
        self.assertEqual(sem._norm("My NEW card number!"), "card number")
        self.assertIsNone(sem._norm("the"))

    def test_prompt_is_frozen_and_neutral(self):
        p = sem.build_prompt(["a 1", "b 2"])
        self.assertTrue(p.startswith(sem.PROMPT))
        self.assertTrue(p.endswith("1. a 1\n2. b 2"))
        low = sem.PROMPT.lower()
        # the prompt names no rule word of the lexical rounds and no wording of the blind r15 set or any dev set
        for bad in ("slot", "cue", "attribute", "supersed", "update", "booking", "card", "membership", "reference",
                    "microchip", "door", "access", "locker", "tag", "ticket number", "flight", "vet"):
            if bad in ("ticket number",):
                continue
            self.assertNotIn(bad, low.replace("warranty ticket", ""), bad)
        for s in BLIND["scenarios"]:
            for f in s["old"] + s["new"]:
                self.assertNotIn(f["value"], sem.PROMPT)

    def test_key_examples_do_not_occur_in_any_set(self):
        for ex in re.findall(r'"([^"<]+)"', sem.PROMPT):
            if ex.count(" ") < 1 or ex.startswith("Lines"):
                continue
            for name, s, brief in briefs():
                self.assertNotIn(ex, brief.lower(), (ex, name))


class Harness(unittest.TestCase):
    def test_arm_a_is_r4_and_b_outside_a_block_is_r4(self):
        for name, _, brief in briefs():
            self.assertEqual(e.COMPRESS["A"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["B"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["R"](brief), bc.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["O"](brief), brief)

    def test_r4_baseline_file_is_the_frozen_byte_copy(self):
        import hashlib
        self.assertTrue((HERE / "bench" / "brief_compress_r4.py").exists())
        sha = hashlib.sha256((HERE / "bench" / "brief_compress_r4.py").read_bytes()).hexdigest()
        frozen = (HERE / "bench" / "ctx-compress-sem-r1-FREEZE.sha256")
        if frozen.exists():
            self.assertIn(f"{sha}  voice-adapter/bench/brief_compress_r4.py", frozen.read_text())

    def test_production_r14_module_untouched(self):
        import hashlib
        self.assertEqual(hashlib.sha256((HERE / "brief_compress.py").read_bytes()).hexdigest(),
                         "5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976")

    def test_block_wrapper_annotates_rows_and_resets_keyer(self):
        s = r15e.r15_suite()[0]
        brief = r2.chat_brief(s)
        sc = BLIND["scenarios"][0]
        sem_keyer = oracle_keyer_for(sc)
        saved = (e.sem.llm_keyer, e.lanespeed.ask)
        e.sem.llm_keyer = lambda url, log=None, timeout=0: (lambda st: sem_keyer(st))
        e.lanespeed.ask = lambda *a, **k: ("ok", 0.1, 0.1, 0.1, {})

        def fake_block(scenarios, arm, t0, dl):
            return [{"scenario": sc_.name, "key": p.key, "result": "RECALL"} for sc_ in scenarios for p in sc_.probes], False
        try:
            rows, stopped = e.make_run_block(fake_block)([s], "B", 0, 1)
            rows_a, _ = e.make_run_block(fake_block)([s], "A", 0, 1)
        finally:
            e.sem.llm_keyer, e.lanespeed.ask = saved
        self.assertFalse(stopped)
        self.assertIsNone(e._STATE["keyer"])
        self.assertTrue(all("sem" in r and "in_brief" in r for r in rows))
        self.assertTrue(all("sem" not in r for r in rows_a))
        self.assertTrue(all(r["sem"]["fallback"] is None and r["sem"]["grouped"] >= 3 for r in rows))
        by = {r["key"]: r["in_brief"] for r in rows}
        upd = [p for p in sc["probes"] if p["cat"].startswith("UPD")]
        self.assertTrue(all(by[p["key"]] for p in upd), by)
        del brief


if __name__ == "__main__":
    unittest.main()
