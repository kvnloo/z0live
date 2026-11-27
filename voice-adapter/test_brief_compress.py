"""C56-live-compress: brief_compress.compress_brief must work on any live chat brief, not just the slm-v2 fixture."""
import json
import re
import unittest
from pathlib import Path

import brief_compress
import slm_bench

HERE = Path(__file__).resolve().parent
SUITE = json.loads((HERE / "bench" / "slm-v2.json").read_text())
FULL = SUITE["brief"]
E5_CHARS = len(json.loads((HERE / "bench" / "slm-v2-e5.json").read_text())["brief"])

# A second, unrelated synthetic chat (different speakers, facts and filler) so the rule is not fixture-bound.
OTHER = "\n".join([
    "Earlier in the chat (oldest first):",
    "Kim: what's the capital of Peru",
    "Assistant: Lima is the capital of Peru. It sits on the Pacific coast and holds about a third of the country's "
    "people, and its old centre is a UNESCO world heritage site with colonial churches and long balconies.",
    "Kim: when is the plumber coming",
    "Assistant: The plumber is booked for Tuesday at 9:15 am, Ortiz Plumbing.",
    "Kim: how do vaccines train the immune system",
    "Assistant: A vaccine shows the immune system a harmless piece or copy of a germ, so B cells learn to make "
    "matching antibodies and T cells learn to spot infected cells. Memory cells stay behind, so a real infection "
    "later meets a fast, trained response instead of a slow first one, which is why most vaccines need a booster.",
    "Kim: you can turn on the porch light schedule, I'm fine with that",
    "Assistant: Noted: porch light schedule is approved.",
    "Kim: explain compound interest like I'm ten",
    "Assistant: If you put ten dollars in a jar that grows by a tenth each year, next year you have eleven, and the "
    "year after you earn a tenth of eleven, not of ten. Your growth starts growing too, so the pile gets bigger "
    "faster the longer you wait, which is why saving early matters so much more than saving a lot later.",
    "Kim: what's the wifi password for the guest network",
    "Assistant: The guest wifi password is lemon-kite-42.",
    "Kim: tips for falling asleep faster",
    "Assistant: Keep the same wake time every day, stop screens half an hour before bed, keep the room cool and "
    "dark, and if you are awake for more than twenty minutes get up and read something dull in dim light until "
    "you feel sleepy, then go back to bed.",
    "Note: car insurance renews on March 3; the router was replaced last week.",
])


def pairs(text):
    lines = text.split("\n")
    return [ln for ln in lines if re.match(r"^[A-Z][\w .'-]{0,30}: ", ln)]


class CompressBrief(unittest.TestCase):
    def test_deterministic_same_bytes(self):
        a = brief_compress.compress_brief(FULL, E5_CHARS)
        for _ in range(3):
            self.assertEqual(brief_compress.compress_brief(FULL, E5_CHARS).encode(), a.encode())
        self.assertEqual(brief_compress.compress_brief(OTHER, 900), brief_compress.compress_brief(OTHER, 900))

    def test_respects_budget(self):
        for text in (FULL, OTHER):
            for budget in (200, 600, 900, 1500, E5_CHARS, 4000, 10000):
                out = brief_compress.compress_brief(text, budget)
                self.assertLessEqual(len(out), budget, (budget, len(out)))
        self.assertEqual(brief_compress.compress_brief(FULL, 10000), FULL)  # already under budget: unchanged

    def test_e5_budget_is_close_to_e5_length(self):
        out = brief_compress.compress_brief(FULL, E5_CHARS)
        self.assertGreater(len(out), 0.8 * E5_CHARS)
        self.assertLess(len(out), 0.6 * len(FULL))

    def test_keeps_header_status_and_newest_turn(self):
        lines = FULL.split("\n")
        out = brief_compress.compress_brief(FULL, E5_CHARS).split("\n")
        self.assertEqual(out[0], lines[0])          # role / header line
        self.assertEqual(out[-1], lines[-1])        # Status line
        # C56-live-compress-r4: the newest exchange (filler) is no longer force-kept; the newest kept exchange is the
        # newest fact exchange (adb approval), verbatim. Was: out[-3:-1] == lines[-3:-1] (newest exchange).
        self.assertEqual(out[-3:-1], lines[-5:-3])
        o = brief_compress.compress_brief(OTHER, 900).split("\n")
        self.assertEqual(o[0], OTHER.split("\n")[0])
        self.assertEqual(o[-1], OTHER.split("\n")[-1])

    def test_keeps_every_planted_fact_and_approval(self):
        out = brief_compress.compress_brief(FULL, E5_CHARS)
        for case in SUITE["cases"]:
            if case["kind"] == "context":
                self.assertRegex(out, re.compile(case["answer"], re.I), case["id"])
        self.assertIn("adb screen access during calls is approved", out)  # standing approval
        o = brief_compress.compress_brief(OTHER, 900)
        for fact in ("Tuesday at 9:15 am", "porch light schedule is approved", "lemon-kite-42", "March 3"):
            self.assertIn(fact, o)

    def test_output_lines_are_original_lines_in_order(self):
        for text, budget in ((FULL, E5_CHARS), (OTHER, 900)):
            src = text.split("\n")
            out = brief_compress.compress_brief(text, budget).split("\n")
            it = iter(src)
            self.assertTrue(all(ln in it for ln in out), "lines must be a verbatim, in-order subset")
            # whole exchanges only: every kept question keeps its reply right after it
            kept = pairs("\n".join(out[1:-1]))  # minus header and trailer (Status:/Note: lines)
            self.assertEqual(len(kept) % 2, 0)
            for q, a in zip(kept[0::2], kept[1::2]):
                self.assertEqual(src[src.index(q) + 1], a)

    def test_drops_filler_before_facts(self):
        out = brief_compress.compress_brief(FULL, 1500)
        self.assertLessEqual(len(out), 1500)
        for case in SUITE["cases"]:
            if case["kind"] == "context":
                self.assertRegex(out, re.compile(case["answer"], re.I), case["id"])

    def test_all_30_cases_lane_request(self):
        small = dict(SUITE, brief=brief_compress.compress_brief(FULL, E5_CHARS))
        self.assertEqual(len(SUITE["cases"]), 30)
        for case in SUITE["cases"]:
            sys_full, ask_full = slm_bench.messages(SUITE, case)
            sys_small, ask_small = slm_bench.messages(small, case)
            self.assertEqual(ask_small, ask_full)
            # tiers.snapshot now compresses itself, so the uncompressed brief is the reference
            self.assertGreater(len(FULL) - len(brief_compress.compress_brief(FULL, E5_CHARS)), 2000, case["id"])
            self.assertLessEqual(len(sys_full), len(sys_small) + 40, case["id"])
            for role, text in case.get("turns") or []:
                self.assertIn(text, sys_small)

    def test_filler_spread_every_other(self):
        # C56-live-compress-r3: filler is every other filler exchange counted back from the newest filler (e5 style),
        # not the newest filler run; facts, header, Status line and newest exchange are still kept.
        header, exchanges, trailer = brief_compress._split(FULL.split("\n"))
        n = len(exchanges)
        fact = [len(" ".join(ex[1:])) <= brief_compress.FACT_REPLY_CHARS for ex in exchanges]
        filler = [i for i in range(n - 1) if not fact[i]]  # oldest -> newest
        out = brief_compress.compress_brief(FULL, E5_CHARS)
        kept = [i for i, ex in enumerate(exchanges) if ex[0] in out.split("\n")]
        kept_filler = [i for i in kept if i in filler]
        self.assertEqual(kept_filler, [3, 6, 9, 12, 15])  # r4: was [6, 9, 12, 15] (newest exchange used budget)
        alternate = filler[::-1][0::2]
        self.assertTrue(set(kept_filler) <= set(alternate))  # never two adjacent fillers at this budget
        for a, b in zip(kept_filler, kept_filler[1:]):
            self.assertEqual(filler.index(b) - filler.index(a), 2)
        self.assertTrue(all(i in kept for i in range(n) if fact[i]))
        self.assertNotIn(n - 1, kept)  # r4: was assertIn (newest exchange force-kept)
        lines = out.split("\n")
        self.assertEqual(lines[0], FULL.split("\n")[0])
        self.assertEqual(lines[-1], FULL.split("\n")[-1])
        self.assertEqual(brief_compress.compress_brief(FULL, E5_CHARS), out)  # deterministic
        # with ample budget the skipped fillers fill in too (newest first), still within budget
        big = brief_compress.compress_brief(FULL, 4000)
        self.assertLessEqual(len(big), 4000)
        self.assertGreater(len(big), len(out))

    def test_newest_not_force_kept_filler_is_e5_set(self):
        # C56-live-compress-r4: the newest exchange is no longer always kept; a filler newest exchange competes last,
        # after the spread filler. At the e5 budget the kept filler is exactly e5's set (3 heat pump in, 17 out).
        header, exchanges, trailer = brief_compress._split(FULL.split("\n"))
        n = len(exchanges)
        out = brief_compress.compress_brief(FULL, E5_CHARS)
        lines = out.split("\n")
        fact = [len(" ".join(ex[1:])) <= brief_compress.FACT_REPLY_CHARS for ex in exchanges]
        kept_filler = [i for i, ex in enumerate(exchanges) if ex[0] in lines and not fact[i]]
        self.assertEqual(kept_filler, [3, 6, 9, 12, 15])
        self.assertFalse(fact[n - 1])
        self.assertNotIn(exchanges[n - 1][0], lines)  # newest (filler) exchange not force-kept
        self.assertTrue(all(exchanges[i][0] in lines for i in range(n) if fact[i]))
        e5 = json.loads((HERE / "bench" / "slm-v2-e5.json").read_text())["brief"]
        self.assertEqual(out, e5)  # byte-identical to the hand-built e5 brief

    def test_degenerate_inputs(self):
        self.assertEqual(brief_compress.compress_brief("", 100), "")
        flat = "x" * 500
        self.assertEqual(brief_compress.compress_brief(flat, 100), flat[-100:])  # no structure: newest chars
        self.assertEqual(brief_compress.compress_brief(FULL, 0), "")


if __name__ == "__main__":
    unittest.main()
