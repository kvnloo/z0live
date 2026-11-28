"""C1-ctx-sem-r2: cached keys, collision judge, displacement rule, r4 fallback. Offline, no model (oracle / failing
stand-ins for the model calls)."""
import hashlib
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_sem as sem1  # noqa: E402
import brief_compress_sem2 as sem2  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import ctx_compress_sem_r2_eval as e  # noqa: E402

BLIND = r15e.BLIND
BUDGET = e.BUDGET
ALL_SETS = ("main",) + r15e.PROBE_SETS
LETTERS = {p["key"]: "".join(chr(103 + int(ch)) if ch.isdigit() else ch for ch in p["key"].replace("_", "")) for s in BLIND["scenarios"] for p in s["probes"]}   # digit-free oracle keys


def briefs():
    for which in ALL_SETS:
        for s in r15e.SETS[which]():
            yield f"{which}/{s.name}", s, r2.chat_brief(s)


def oracle_for(scn):
    """Oracle key1 / judge for one blind scenario: the values of one update share a key and are judged REPLACE; the
    still-true pair shares a key too (a forced collision) and is judged ALSO; everything else gets a unique key."""
    group, upd = {}, set()
    pair = [p for p in scn["probes"] if p["cat"] in ("TWO_OLD", "TWO_NEW")]
    for p in scn["probes"]:
        if p["cat"].startswith("UPD"):
            for v in [p["current"], *p["superseded"]]:
                group[v] = "fact " + LETTERS[p["key"]]
                upd.add(v)
    for p in pair:
        group[p["current"]] = "pair fact"

    def which(s):
        hit = [v for v in group if v in s]
        return hit[0] if hit else None

    def key1(s):
        v = which(s)
        return group[v] if v else "unique " + "".join(chr(97 + int(c, 16)) for c in sem2.stmt_hash(s)[:10])

    def judge(older, newer):
        a, b = which(older), which(newer)
        return "replace" if a in upd and b in upd and group[a] == group[b] else "also"
    return key1, judge


def warm(brief, cache, budget=BUDGET):
    earlier = []
    for st in sem2.statements(brief, budget):
        cache.arrive(st, earlier)
        earlier.append(st)


class Fallback(unittest.TestCase):
    def test_no_cache_is_r4_byte_identical_everywhere(self):
        n = 0
        for name, _, brief in briefs():
            self.assertEqual(sem2.compress_brief_sem2(brief, BUDGET), r4.compress_brief(brief, BUDGET), name)
            n += 1
        self.assertGreaterEqual(n, 18)

    def test_failing_or_distinct_or_also_is_r4_byte_identical(self):
        def boom(*_):
            raise TimeoutError("timed out")
        cases = ((boom, boom), (lambda s: None, boom), (lambda s: "k " + sem2.stmt_hash(s)[:12], boom),
                 (lambda s: "same thing", lambda o, n: "also"), (lambda s: "same thing", lambda o, n: "maybe"))
        for key1, judge in cases:
            for name, _, brief in briefs():
                c = sem2.KeyCache(key1, judge)
                warm(brief, c)
                out, info = sem2.compress_brief_sem2_info(brief, BUDGET, c, judge)
                self.assertEqual(out, r4.compress_brief(brief, BUDGET), name)
                self.assertEqual(info["replaced"], 0, name)

    def test_within_budget_and_degenerate(self):
        c = sem2.KeyCache(lambda s: 1 / 0, lambda o, n: 1 / 0)
        self.assertEqual(sem2.compress_brief_sem2("short", 100, c), "short")
        self.assertEqual(sem2.compress_brief_sem2("", 100, c), "")
        self.assertEqual(sem2.compress_brief_sem2("x" * 50, 0, c), "")
        no_speakers = "line\n" * 100
        self.assertEqual(sem2.compress_brief_sem2(no_speakers, 40, c), r4.compress_brief(no_speakers, 40))


class Cache(unittest.TestCase):
    def test_key_once_per_normalised_statement(self):
        calls = []
        c = sem2.KeyCache(lambda s: calls.append(s) or "locker code")
        self.assertEqual(c.add("My locker  is Oak-1.")[0], "locker code")
        self.assertIsNone(c.add(" My locker is\tOak-1. ")[1])   # whitespace-normalised: a cache hit, no call
        self.assertEqual(calls, ["My locker is Oak-1."])
        self.assertEqual(c.lookup("My locker is Oak-1."), (True, "locker code"))
        self.assertEqual(c.lookup("My locker is Oak-2."), (False, None))
        self.assertEqual(sem2.stmt_hash("a  b"), hashlib.sha256(b"a b").hexdigest())

    def test_warm_brief_makes_no_model_call(self):
        _, s, brief = next(b for b in briefs() if b[0] == "r15/r15-vet")
        key1, judge = oracle_for(BLIND["scenarios"][0])
        calls = []
        c = sem2.KeyCache(lambda st: calls.append(1) or key1(st), lambda o, n: calls.append(2) or judge(o, n))
        warm(brief, c)
        n_arrival = len(calls)
        self.assertGreater(n_arrival, 0)
        out, info = sem2.compress_brief_sem2_info(brief, BUDGET, c, lambda o, n: 1 / 0)
        self.assertEqual(len(calls), n_arrival)
        self.assertEqual((info["key_misses"], info["judge_misses"]), (0, 0))
        self.assertGreater(info["replaced"], 0)

    def test_cold_brief_keys_and_judges_misses(self):
        _, s, brief = next(b for b in briefs() if b[0] == "r15/r15-car")
        key1, judge = oracle_for(BLIND["scenarios"][1])
        c = sem2.KeyCache(key1)
        out, info = sem2.compress_brief_sem2_info(brief, BUDGET, c, judge)
        self.assertEqual(info["key_misses"], info["candidates"])
        self.assertEqual(info["judge_misses"], info["collisions"])
        c2 = sem2.KeyCache(key1, judge)
        warm(brief, c2)
        self.assertEqual(sem2.compress_brief_sem2(brief, BUDGET, c2), out)


class Collision(unittest.TestCase):
    def test_collides(self):
        self.assertEqual(sem2.collides("locker code", "locker code"), 2)
        self.assertEqual(sem2.collides("drink code", "interval drinks code"), 1)
        self.assertEqual(sem2.collides("shed key", "shed key tag"), 1)
        self.assertEqual(sem2.collides("locker tag neon 29", "locker tag neon"), 2)
        self.assertEqual(sem2.collides("code", "door code"), 0)   # a 1-word key never matches as a subset
        self.assertEqual(sem2.collides("door code", "access code"), 0)
        self.assertEqual(sem2.collides(None, "x"), 0)
        self.assertEqual(sem2.collides("glass", "glass"), 2)

    def test_partner_prefers_latest_equal(self):
        self.assertEqual(sem2.partner(["a b", "a b c", "a b", "x"], 1), 0)
        self.assertEqual(sem2.partner(["a b", "a b c", "a b c d", "a b c"], 3), 1)
        self.assertEqual(sem2.partner(["a b", "z"], 1), None)

    def test_judge_only_on_collisions_and_also_keeps_both(self):
        lines = ["Brief header"]
        for u, a in (("My locker is Oak-1.", "Noted."), ("Filler about the weather and many other things.",
                                                         "x" * 260), ("My other locker is Oak-2, see you.", "Noted."),
                     ("My parking spot is P-9.", "Noted.")):
            lines += [f"User: {u}", f"Assistant: {a}"]
        text = "\n".join(lines)
        pairs = []
        keys = {"Oak-1": "locker", "Oak-2": "locker", "P-9": "parking spot"}
        c = sem2.KeyCache(lambda s: next(k for v, k in keys.items() if v in s),
                          lambda o, n: pairs.append((o, n)) or "also")
        warm(text, c, len(text) - 40)
        self.assertEqual(len(pairs), 1)
        out = sem2.compress_brief_sem2(text, len(text) - 40, c)
        self.assertEqual(out, r4.compress_brief(text, len(text) - 40))
        c.verdicts.clear()
        c.judge = lambda o, n: "replace"
        warm(text, c, len(text) - 40)
        out = sem2.compress_brief_sem2(text, len(text) - 40, c)
        self.assertIn("Oak-2", out)
        self.assertNotIn("Oak-1", out)
        self.assertTrue(set(out.split("\n")) <= set(text.split("\n")))

    def test_parsers(self):
        self.assertEqual(sem2.parse_key("Key: Locker Code.\nmore"), "locker code")
        self.assertEqual(sem2.parse_key("\n  \"the new card number\"  "), "card number")
        self.assertIsNone(sem2.parse_key(""))
        self.assertEqual(sem2.parse_judge("REPLACE"), "replace")
        self.assertEqual(sem2.parse_judge("also."), "also")
        self.assertEqual(sem2.parse_judge("Also, not replace"), "also")
        self.assertIsNone(sem2.parse_judge("unsure"))

    def test_prompts_are_neutral(self):
        for p in (sem2.KEY_PROMPT, sem2.JUDGE_PROMPT):
            low = p.lower()
            for bad in ("slot", "cue", "attribute", "supersed", "booking", "card", "membership", "reference",
                        "microchip", "door", "access", "locker", "flight", "vet", "pottery", "car "):
                self.assertNotIn(bad, low, bad)
            for s in BLIND["scenarios"]:
                for f in s["old"] + s["new"]:
                    self.assertNotIn(f["value"], p)
        for ex in re.findall(r'"([^"<]+)"', sem2.KEY_PROMPT):
            for name, s, brief in briefs():
                self.assertNotIn(ex, brief.lower(), (ex, name))


class Displacement(unittest.TestCase):
    def test_oracle_bound_on_blind_set(self):
        """Perfect keys + perfect judge: every update CURRENT and never stale, pair-old and EVERY static kept (sem-r1's
        oracle lost car s1 to budget displacement; the reservation rule keeps it)."""
        for i, scn in enumerate(BLIND["scenarios"]):
            s = r15e.r15_suite()[i]
            brief = r2.chat_brief(s)
            key1, judge = oracle_for(scn)
            c = sem2.KeyCache(key1, judge)
            warm(brief, c)
            out, info = sem2.compress_brief_sem2_info(brief, BUDGET, c)
            a = r4.compress_brief(brief, BUDGET)
            self.assertLessEqual(len(out), BUDGET)
            self.assertTrue(set(out.split("\n")) <= set(brief.split("\n")))
            for p in scn["probes"]:
                if p["cat"] in ("TWO_OLD", "STATIC"):
                    if p["current"] in a:
                        self.assertIn(p["current"], out, (scn["name"], p["key"]))
                elif p["cat"].startswith("UPD"):
                    for v in p["superseded"]:
                        self.assertNotIn(v, out, (scn["name"], p["key"]))

    def test_r4_kept_static_facts_are_never_displaced(self):
        for name, s, brief in briefs():
            k = lambda st: "same thing"  # noqa: E731 - worst case: everything collides and is replaced
            c = sem2.KeyCache(k, lambda o, n: "replace")
            warm(brief, c)
            out, info = sem2.compress_brief_sem2_info(brief, BUDGET, c)
            self.assertLessEqual(len(out), BUDGET, name)
            self.assertTrue(set(out.split("\n")) <= set(brief.split("\n")), name)

    def test_rule_off_can_displace_and_rule_on_cannot(self):
        lines = ["Brief header"]
        for u in ("My locker is Oak-1.", "My bike in the rack is B-2, the blue one.",
                  "My locker is now Oak-22, upstairs near the north window."):
            lines += [f"User: {u}", "Assistant: Noted."]
        text = "\n".join(lines)
        budget = len("\n".join(lines[:5])) + 2
        keys = {"Oak-1": "locker", "Oak-22": "locker", "B-2": "bike"}
        c = sem2.KeyCache(lambda s: next(k for v, k in keys.items() if v in s), lambda o, n: "replace")
        warm(text, c, budget)
        on = sem2.compress_brief_sem2(text, budget, c)
        off = sem2.compress_brief_sem2_info(text, budget, c, None, displacement_rule=False)[0]
        self.assertIn("B-2", r4.compress_brief(text, budget))
        self.assertIn("B-2", on)
        self.assertNotIn("Oak-1", on)
        self.assertIn("Oak-22", off)
        self.assertNotIn("B-2", off)


class Harness(unittest.TestCase):
    def test_arms_outside_a_block(self):
        for name, _, brief in briefs():
            self.assertEqual(e.COMPRESS["A"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["B"](brief), r4.compress_brief(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["R"](brief), sem1.compress_brief_sem(brief, BUDGET), name)
            self.assertEqual(e.COMPRESS["O"](brief), brief)

    def test_frozen_modules_untouched(self):
        sha = {"brief_compress.py": "5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976"}
        for f, h in sha.items():
            self.assertEqual(hashlib.sha256((HERE / f).read_bytes()).hexdigest(), h, f)
        frozen = HERE / "bench" / "ctx-compress-sem-r2-FREEZE.sha256"
        if frozen.exists():
            for rel in ("bench/brief_compress_r4.py", "brief_compress_sem.py", "bench/ctx_compress_r15_blind.json"):
                h = hashlib.sha256((HERE / rel).read_bytes()).hexdigest()
                self.assertIn(f"{h}  voice-adapter/{rel}", frozen.read_text())

    def test_block_wrapper_annotates_rows_and_resets(self):
        s = r15e.r15_suite()[0]
        sc = BLIND["scenarios"][0]
        key1, judge = oracle_for(sc)
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
        self.assertIsNone(e._STATE["cache"])
        self.assertTrue(all("sem" in r and "in_brief" in r and "in_brief_ablation" in r for r in rows))
        self.assertTrue(all("sem" not in r for r in rows_a))
        for r in rows:
            self.assertEqual((r["sem"]["key_misses"], r["sem"]["judge_misses"]), (0, 0))
            self.assertGreater(len(r["sem"]["arrival_walls"]), 0)
            self.assertGreaterEqual(r["sem"]["replaced"], 3)
        by = {r["key"]: r["in_brief"] for r in rows}
        for p in sc["probes"]:
            if p["cat"] in ("TWO_OLD", "STATIC"):
                self.assertTrue(by[p["key"]], (p["key"], by))


if __name__ == "__main__":
    unittest.main()
