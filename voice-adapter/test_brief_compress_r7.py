"""C1-ctx-compress-r7: supersession-aware fact key (the ONE change). Of two or more fact exchanges filling the same
slot (the asker's own "my <subject> is ..." statement, same normalised subject) only the newest is tried, at the
shortest size among the slot's members; the older members are dropped. Every other fact keeps r4's strict
shortest-first order (equal sizes newest first).

bench/brief_compress_r4.py is the frozen r4 compress_brief (sha256 5a88bc8b...). Behaviour tests where r7 differs
are red on r4 and green on r7.
C1-ctx-compress-r9: these tests now run on bench/brief_compress_r7.py (frozen r7 byte copy); r9 tests:
test_brief_compress_r9.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress_r7 as brief_compress  # noqa: E402  (r9: r7 semantics pinned to the frozen r7 copy)
import brief_compress_r4 as r4  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
import ctx_compress_r6_eval as r6eval  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

PAD = "x" * 60


def toy(unique=False):
    # old value (shorter) first, two note facts, then the newer value of the same slot (longer). Optional old unique
    # fact (different slot), the same size as the old value.
    lines = ["H",
             "User: My tea blend is Rose-21. " + PAD,
             "Assistant: ok"]
    if unique:
        lines += ["User: My shelf label is Iris-55. " + PAD[:58], "Assistant: ok"]
    lines += ["User: note one " + PAD + "y" * 18, "Assistant: ok",
              "User: note two " + PAD + "y" * 18, "Assistant: ok",
              "User: Changed: my tea blend is now Sage-48. " + PAD[:50], "Assistant: ok"]
    return "\n".join(lines)


def exchanges(text):
    return brief_compress._split(text.split("\n"))[1]


class Slot(unittest.TestCase):
    def test_slot_extraction(self):
        cases = [("User: My workshop bench number is Fern-21.", "workshop bench number"),
                 ("User: Update: my workshop bench number is now Moss-48, not the old one.", "workshop bench number"),
                 ("user: my   Favourite  Mug is blue", "favourite mug"),
                 ("User: My kids are at school.", "kids"),
                 ("User: I just got the parcel pickup code: Elm-24.", None),
                 ("User: Change of plan: my flight moved to Thursday.", None),
                 ("User: Workshop note 3: the felt sample has a subtle texture.", None)]
        for line, want in cases:
            self.assertEqual(brief_compress._slot([line, "Assistant: ok"]), want, line)

    def test_no_false_merge(self):
        # different subjects (including a prefix subject) are different slots; both members are kept
        ex = [["User: My bench is Fern-21."], ["User: My bench number is Moss-48."],
              ["User: My badge code is Teal-13."], ["User: My badge colour code is Plum-64."]]
        self.assertEqual(len({brief_compress._slot(e) for e in ex}), 4)
        self.assertEqual(sorted(brief_compress._fact_order(ex, [0, 1, 2, 3])), [0, 1, 2, 3])
        text = "\n".join(["H"] + [ln for e in ex for ln in (e[0], "Assistant: ok")] + ["User: " + "z" * 300])
        out = brief_compress.compress_brief(text, 200)
        for v in ("Fern-21", "Moss-48", "Teal-13", "Plum-64"):
            self.assertIn(v, out)

    def test_order_is_r4_without_shared_slot(self):
        ex = [["User: a" * 3], ["User: My cup is Oak-1. " + "b" * 20], ["User: c" * 3], ["User: d" * 12]]
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2, 3]),
                         sorted(reversed(range(4)), key=lambda i: brief_compress._size([ex[i]])))


class Supersede(unittest.TestCase):
    def test_same_slot_newer_wins(self):
        text = toy()
        sizes = [brief_compress._size([e]) for e in exchanges(text)]
        self.assertLess(sizes[0], sizes[1])                  # old < notes < new
        self.assertLess(sizes[1], sizes[3])
        budget = len("H") + 1 + sizes[3] + 1 + sizes[1]     # room for the new value and one note only
        out = brief_compress.compress_brief(text, budget)
        self.assertIn("Sage-48", out)
        self.assertNotIn("Rose-21", out)
        old = r4.compress_brief(text, budget)                # r4: shortest first keeps the stale value (red on r4)
        self.assertIn("Rose-21", old)
        self.assertNotIn("Sage-48", old)

    def test_old_unique_fact_preserved(self):
        text = toy(unique=True)
        sizes = [brief_compress._size([e]) for e in exchanges(text)]
        self.assertEqual(sizes[0], sizes[1])                 # unique fact the same size as the old value
        budget = len("H") + 1 + sizes[1] + 1 + sizes[4] + 1 + sizes[2]
        out = brief_compress.compress_brief(text, budget)
        self.assertIn("Iris-55", out)                        # old unique fact kept (r6 lost this case)
        self.assertIn("Sage-48", out)
        self.assertNotIn("Rose-21", out)
        old = r4.compress_brief(text, budget)
        self.assertIn("Iris-55", old)
        self.assertNotIn("Sage-48", old)

    def test_three_members_only_newest(self):
        lines = ["H"]
        for v in ("Ash-1", "Ash-22", "Ash-333"):
            lines += [f"User: my locker code is {v}.", "Assistant: ok"]
        out = brief_compress.compress_brief("\n".join(lines + ["User: " + "q" * 200]), 120)
        self.assertIn("Ash-333", out)
        self.assertNotIn("Ash-1.", out)
        self.assertNotIn("Ash-22", out)

    def test_recency_suite_dry_prediction(self):
        # PREREG dry run (recorded, not a goal): r4 keeps OLD bench/badge + glaze; r7 keeps NEW bench/badge + glaze,
        # never OLD, in all 3 recency scenarios.
        for s in r6eval.recency_suite():
            b = harness.chat_brief(s)
            a, r7 = r4.compress_brief(b, harness.BUDGET), brief_compress.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(r7), harness.BUDGET)
            for p in s.probes:
                if p.superseded:
                    self.assertIn(p.superseded[0], a)
                    self.assertNotIn(p.current, a)
                    self.assertIn(p.current, r7)
                    self.assertNotIn(p.superseded[0], r7)
                else:
                    self.assertIn(p.current, a)
                    self.assertIn(p.current, r7)


class Differential(unittest.TestCase):
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

    def test_main_set_identical_recency_differs(self):
        main = [brief_compress.compress_brief(harness.chat_brief(s), harness.BUDGET)
                == r4.compress_brief(harness.chat_brief(s), harness.BUDGET) for s in harness.suite()]
        rec = [brief_compress.compress_brief(harness.chat_brief(s), harness.BUDGET)
               == r4.compress_brief(harness.chat_brief(s), harness.BUDGET) for s in r6eval.recency_suite()]
        self.assertEqual((main, rec), ([True] * 3, [False] * 3))


# recorded: r7 is byte-identical to r4 on FULL, OTHER, slm-v2-e5 and slm-v2-live at every r2 budget
DIFF_VS_R4 = [[], [], [], []]

if __name__ == "__main__":
    unittest.main()
