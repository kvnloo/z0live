"""C1-ctx-compress-r4: facts are tried by density, shortest exchange first (the ONE change; was newest first).

bench/brief_compress_r3.py is the frozen r3 compress_brief (byte copy of brief_compress.py at 10ad7ed,
sha256 9f6ba9c2...). Recorded differential vs r3 at the r2 BUDGETS: FULL, slm-v2-e5 and slm-v2-live differ only at
500; OTHER is byte-identical; the three context_eval briefs differ (that is the change under test).
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_r3 as r3  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

LONG = "That is a long general explanation of how the sample textures compare under the lamp. " * 2  # ~180 chars


def toy():
    # oldest: a reply-less user fact; newer: short-reply facts whose ~180-char replies are under FACT_REPLY_CHARS
    lines = ["Synthetic header line", "User: My vendor code is Mint-57."]
    for i in range(8):
        lines += [f"User: note {i} about sample textures", "Assistant: " + LONG.strip()]
    return "\n".join(lines)


class FactsByDensity(unittest.TestCase):
    def test_old_short_fact_beats_newer_long_reply_facts(self):
        text = toy()
        reply = len(text.split("\n")[3]) - len("Assistant: ")
        self.assertLessEqual(reply, brief_compress.FACT_REPLY_CHARS)  # the long pairs are facts too
        out = brief_compress.compress_brief(text, 700)
        self.assertLessEqual(len(out), 700)
        self.assertIn("User: My vendor code is Mint-57.", out.split("\n"))
        self.assertNotIn("Mint-57", r3.compress_brief(text, 700))  # r3: newest-first facts fill the budget first

    def test_equal_sizes_stay_newest_first(self):
        lines = ["H"]
        for i in range(6):
            lines += [f"User: fact {i}", "Assistant: ok"]
        text = "\n".join(lines)
        out = brief_compress.compress_brief(text, 85).split("\n")
        kept = [ln for ln in out if ln.startswith("User: ")]
        self.assertEqual(kept, ["User: fact 3", "User: fact 4", "User: fact 5"])

    def test_r2_harness_dry_prediction(self):
        # PREREG dry-run (recorded, not a goal): at 2810 B keeps all four reply-less turns (vendor, flight-old, room,
        # flight-new) and the stable header in all 3 scenarios; r3 kept only room and flight-new.
        for s in harness.suite():
            b = harness.chat_brief(s)
            out = brief_compress.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(out), harness.BUDGET)
            self.assertTrue(out.startswith(s.stable + "\n"))
            ex = brief_compress._split(b.split("\n"))[1]
            lines = out.split("\n")
            kept = {i for i, x in enumerate(ex) if x[0] in lines}
            self.assertEqual(kept & {0, 1, 5, 6}, {0, 1, 5, 6})
            vendor = [p.current for p in s.probes if p.key == "vendor"][0]
            self.assertIn(vendor, out)
            self.assertNotIn(vendor, r3.compress_brief(b, harness.BUDGET))
            self.assertEqual([ln for ln in b.split("\n") if ln in lines], lines)  # verbatim, in order

    def test_recorded_differential_vs_r3(self):
        briefs = capitalised_briefs()
        diff = [[b for b in BUDGETS if brief_compress.compress_brief(t, b) != r3.compress_brief(t, b)]
                for t in briefs[:4]]
        self.assertEqual(diff, [[500], [], [500], [500]])  # FULL, OTHER, slm-v2-e5, slm-v2-live

    def test_budget_and_verbatim_everywhere(self):
        for text in capitalised_briefs():
            src = text.split("\n")
            for b in BUDGETS:
                out = brief_compress.compress_brief(text, b)
                self.assertLessEqual(len(out), b)
                if out and out != text[-b:]:
                    it = iter(src)
                    self.assertTrue(all(ln in it for ln in out.split("\n")))


if __name__ == "__main__":
    unittest.main()
