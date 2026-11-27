"""C1-ctx-compress-r6: tiered fact key (the ONE change). Facts are tried shortest size band first; inside a band
(every fact up to 10% longer than the band's shortest) newest first. Was (r4): strict size ascending.

bench/brief_compress_r4.py is the frozen r4 compress_brief (byte copy of brief_compress.py at c96fe4b, sha256
5a88bc8b...). Each behaviour test is red on r4 and green on r6 where r6 differs.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress_r6 as brief_compress  # noqa: E402  C1-ctx-compress-r7: r6 code is now bench/brief_compress_r6.py
import brief_compress_r4 as r4  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
import ctx_compress_r6_eval as r6eval  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

PAD = "x" * 60


def recency_toy():
    # old value (shorter) first, then two filler-sized facts, then the newer value 5% longer (same band)
    lines = ["H",
             "User: my bench is Fern-21. " + PAD,
             "Assistant: ok",
             "User: note one " + PAD + "y" * 14,
             "Assistant: ok",
             "User: note two " + PAD + "y" * 14,
             "Assistant: ok",
             "User: update: my bench is now Moss-48. " + PAD[:52],
             "Assistant: ok"]
    return "\n".join(lines)


class TieredFactKey(unittest.TestCase):
    def test_recency_tie_newer_wins(self):
        text = recency_toy()
        ex = brief_compress._split(text.split("\n"))[1]
        sizes = [brief_compress._size([e]) for e in ex]
        self.assertLess(sizes[0], sizes[1])                       # older < notes < newer
        self.assertLess(sizes[1], sizes[3])
        self.assertLessEqual(sizes[3], sizes[0] * 1.10)           # all inside one 10% band
        budget = len("H") + 1 + sizes[3] + 1 + sizes[2]          # room for two band members only
        out = brief_compress.compress_brief(text, budget)
        self.assertIn("Moss-48", out)
        self.assertNotIn("Fern-21", out)
        old = r4.compress_brief(text, budget)                     # r4: shortest first keeps the stale value
        self.assertIn("Fern-21", old)
        self.assertNotIn("Moss-48", old)

    def test_outside_band_stays_shortest_first(self):
        # a one-line fact far shorter than newer long facts is a band of its own and is still tried first (r4 fix)
        lines = ["H", "User: My vendor code is Mint-57."]
        for i in range(8):
            lines += [f"User: note {i} about sample textures", "Assistant: " + "long explanation " * 10]
        text = "\n".join(lines)
        out = brief_compress.compress_brief(text, 700)
        self.assertIn("Mint-57", out)
        self.assertLessEqual(len(out), 700)

    def test_band_edge(self):
        ex = [["a" * 100], ["b" * 110], ["c" * 111], ["d" * 50]]
        # sizes 100, 110, 111, 50 -> bands [50], [100, 110] (110 <= 110.0), [111]
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2, 3]), [3, 1, 0, 2])

    def test_order_is_r4_when_no_band_has_two_sizes(self):
        ex = [["a" * 10], ["b" * 30], ["c" * 10], ["d" * 90]]
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2, 3]), [2, 0, 1, 3])

    def test_equal_sizes_newest_first_unchanged(self):
        lines = ["H"]
        for i in range(6):
            lines += [f"User: fact {i}", "Assistant: ok"]
        text = "\n".join(lines)
        out = brief_compress.compress_brief(text, 85).split("\n")
        self.assertEqual([ln for ln in out if ln.startswith("User: ")], ["User: fact 3", "User: fact 4", "User: fact 5"])

    def test_r2_harness_planted_values_kept(self):
        for s in harness.suite():
            b = harness.chat_brief(s)
            out = brief_compress.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(out), harness.BUDGET)
            self.assertTrue(out.startswith(s.stable + "\n"))
            vendor = [p.current for p in s.probes if p.key == "vendor"][0]
            self.assertIn(vendor, out)
            lines = out.split("\n")
            self.assertEqual([ln for ln in b.split("\n") if ln in lines], lines)

    def test_recency_suite_dry_prediction(self):
        # PREREG dry-run (recorded, not a goal): r4 keeps the OLD bench/badge values and the glaze fact and drops the
        # NEW values; r6 keeps the NEW values and drops the OLD values and the glaze fact, in all 3 scenarios.
        for s in r6eval.recency_suite():
            b = harness.chat_brief(s)
            got = {name: f(b, harness.BUDGET) for name, f in (("r4", r4.compress_brief),
                                                               ("r6", brief_compress.compress_brief))}
            for p in s.probes:
                if p.superseded:
                    self.assertIn(p.superseded[0], got["r4"])
                    self.assertNotIn(p.current, got["r4"])
                    self.assertIn(p.current, got["r6"])
                    self.assertNotIn(p.superseded[0], got["r6"])
                else:
                    self.assertIn(p.current, got["r4"])
                    self.assertNotIn(p.current, got["r6"])

    def test_budget_and_verbatim_everywhere(self):
        for text in capitalised_briefs():
            src = text.split("\n")
            for b in BUDGETS:
                out = brief_compress.compress_brief(text, b)
                self.assertLessEqual(len(out), b)
                if out and out != text[-b:]:
                    it = iter(src)
                    self.assertTrue(all(ln in it for ln in out.split("\n")))

    def test_recorded_differential_vs_r4(self):
        briefs = capitalised_briefs()
        diff = [[b for b in BUDGETS if brief_compress.compress_brief(t, b) != r4.compress_brief(t, b)]
                for t in briefs[:4]]
        self.assertEqual(diff, DIFF_VS_R4)


# recorded: r6 is byte-identical to r4 on FULL, OTHER, slm-v2-e5 and slm-v2-live at every r2 budget
DIFF_VS_R4 = [[], [], [], []]

if __name__ == "__main__":
    unittest.main()
