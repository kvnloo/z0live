"""C1-ctx-compress-r11: attribute-aware supersession (the ONE change).

r9 (bench/brief_compress_r9.py, sha256 cb1cb6b8...) let a newer same-slot member with an update cue drop every older
member of the "my <subject> is" slot. r10 showed this false-merges a different attribute that happens to carry a cue
("my kayak is now at the repair bay" dropped "my kayak is the yellow one tagged X"). r11 groups by attribute =
(slot subject, predicate head) instead of the slot subject alone (brief_compress._attribute, frozen in the r11
PREREG); within a group the r9 cue rule is unchanged. No cue, or a cue statement about a different attribute, gives
r4's order and output byte for byte (bench/brief_compress_r4.py, sha256 5a88bc8b...).
"""
import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress as bc  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_r9 as r9  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
import ctx_compress_r6_eval as r6eval  # noqa: E402
import ctx_compress_r8_eval as r8eval  # noqa: E402
import ctx_compress_r10_eval as r10eval  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

R4_SHA256 = "5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64"
R9_SHA256 = "cb1cb6b891878203314559d3562e852b49b596ed1ae969b2dcdecaf584a0323c"
PAD = "x" * 60


def attr(line):
    return bc._attribute([line, "Assistant: ok"])


def toy(update):
    # old value (shorter) first, two note facts, then a newer member of the same slot (longer)
    return "\n".join(["H",
                      "User: My kayak is the yellow one tagged Rowan-33. " + PAD, "Assistant: ok",
                      "User: note one " + PAD + "y" * 30, "Assistant: ok",
                      "User: note two " + PAD + "y" * 30, "Assistant: ok",
                      "User: " + update + " " + PAD[:40], "Assistant: ok"])


def exchanges(text):
    return bc._split(text.split("\n"))[1]


def r4_order(ex, idx):
    # r4's fact key: size ascending, newest first on equal sizes
    return sorted(reversed(idx), key=lambda i: bc._size([ex[i]]))


class Attribute(unittest.TestCase):
    def test_value_head(self):
        for line in ("User: My boathouse locker number is Poplar-23.",
                     "User: Quick note, my boathouse locker number is Spruce-47 instead, after the refit.",
                     "User: Update: my boathouse locker number is Spruce-47, the system changed over.",
                     "User: my boathouse locker number is updated to Spruce-47, the coach relabelled them.",
                     "User: My boathouse locker number is no longer the old one, it is Spruce-47 after the recheck.",
                     "User: Update: my boathouse locker number is now Spruce-47, not the old one."):
            self.assertEqual(attr(line), ("boathouse locker number", bc.VALUE), line)

    def test_different_attribute_heads(self):
        self.assertEqual(attr("User: My kayak is the yellow one tagged Rowan-33."), ("kayak", "yellow"))
        self.assertEqual(attr("User: My kayak is now at the repair bay, the ticket is Hazel-58."), ("kayak", "at"))
        self.assertEqual(attr("User: My rope was moved to the back store, the ticket is Holly-75."), ("rope", "back"))
        self.assertEqual(attr("User: My tote bag is at the new cloakroom desk today."), ("tote bag", "at"))
        self.assertEqual(attr("User: My harness is now hanging in the cage."), ("harness", "hanging"))

    def test_no_slot(self):
        self.assertIsNone(attr("User: We switched to new saddle bags, the tag says Oak-1 now."))
        self.assertIsNone(attr("User: note one"))

    def test_label_and_reply_ignored(self):
        self.assertEqual(bc._attribute(["My: my cup is Oak-1.", "Assistant: my cup is the blue one"]),
                         ("cup", bc.VALUE))


class Supersede(unittest.TestCase):
    def test_same_attribute_cue_update_drops_older(self):
        ex = [["User: my locker code is Ash-1."], ["User: note " + "n" * 40],
              ["User: my locker code is now Ash-22, not the old one."]]
        order = bc._fact_order(ex, [0, 1, 2])
        self.assertNotIn(0, order)
        self.assertEqual(order[0], 2)                       # takes the older member's shorter size
        self.assertEqual(order, r9._fact_order(ex, [0, 1, 2]))  # same attribute: r11 acts as r9
        self.assertEqual(r4_order(ex, [0, 1, 2]), [0, 1, 2])  # red on r4

    def test_same_attribute_end_to_end(self):
        text = toy("Changed: my kayak is now the red one tagged Owl-9.")  # head "red" != "yellow": NOT same attr
        self.assertNotEqual(attr(exchanges(text)[0][0]), attr(exchanges(text)[3][0]))
        text = toy("My kayak is now Rowan-44 instead.")
        ex = exchanges(text)
        self.assertEqual(attr("User: My kayak is Rowan-33."), attr(ex[3][0]))
        same = "\n".join(["H", "User: My kayak is Rowan-33. " + PAD, "Assistant: ok",
                          "User: note one " + PAD + "y" * 30, "Assistant: ok",
                          "User: My kayak is now Rowan-44 instead. " + PAD, "Assistant: ok"])
        ex = exchanges(same)
        budget = 1 + 1 + bc._size([ex[2]])  # header + the newer member only
        out = bc.compress_brief(same, budget)
        self.assertIn("Rowan-44", out)
        self.assertNotIn("Rowan-33", out)
        self.assertIn("Rowan-33", r4.compress_brief(same, budget))  # red on r4

    def test_different_attribute_cue_keeps_both(self):
        text = toy("My kayak is now at the repair bay by the slipway, the repair ticket on it is Hazel-58.")
        ex = exchanges(text)
        idx = list(range(len(ex)))
        self.assertEqual(bc._slot(ex[0]), bc._slot(ex[3]))   # shared lexical slot
        self.assertTrue(bc._cue(ex[3]))                       # cue word present
        self.assertEqual(bc._fact_order(ex, idx), r4_order(ex, idx))
        self.assertNotEqual(r9._fact_order(ex, idx), bc._fact_order(ex, idx))  # red on r9
        for b in range(1, len(text) + 2):
            self.assertEqual(bc.compress_brief(text, b), r4.compress_brief(text, b), b)
        self.assertEqual(sorted(bc._fact_order(ex, idx)), idx)       # nothing dropped: both facts stay candidates
        self.assertNotIn(0, r9._fact_order(ex, idx))                  # r9 dropped the older fact
        tight = len(text) - 1
        self.assertIn("Rowan-33", bc.compress_brief(text, tight))
        self.assertNotIn("Rowan-33", r9.compress_brief(text, tight))

    def test_no_cue_is_r4(self):
        text = toy("My kayak is Rowan-44 for the spare seat.")
        ex = exchanges(text)
        idx = list(range(len(ex)))
        self.assertEqual(bc._fact_order(ex, idx), r4_order(ex, idx))
        for b in range(1, len(text) + 2):
            self.assertEqual(bc.compress_brief(text, b), r4.compress_brief(text, b), b)

    def test_newest_same_attribute_cue_wins(self):
        ex = [["User: my locker code is Ash-1."], ["User: my locker code is at the gym now."],
              ["User: my locker code is now Ash-333."]]
        order = bc._fact_order(ex, [0, 1, 2])
        self.assertEqual(sorted(order), [1, 2])  # Ash-333 drops Ash-1; the "at" statement is another attribute


class Differential(unittest.TestCase):
    def test_bench_copies(self):
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r4.py").read_bytes()).hexdigest(), R4_SHA256)
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r9.py").read_bytes()).hexdigest(), R9_SHA256)

    def test_r4_byte_identical_on_existing_fixtures(self):
        # FULL, OTHER, slm-v2-e5, slm-v2-live and the context_eval briefs, at every r2 budget
        for text in capitalised_briefs():
            for b in BUDGETS:
                self.assertEqual(bc.compress_brief(text, b), r4.compress_brief(text, b), b)

    def test_main_set_identical_to_r4(self):
        for s in harness.suite():
            b = harness.chat_brief(s)
            self.assertEqual(bc.compress_brief(b, harness.BUDGET), r4.compress_brief(b, harness.BUDGET))

    def test_recency_and_r8_identical_to_r9(self):
        # every superseding member there restates the value ("is now X"): same attribute, r11 = r9
        for s in r6eval.recency_suite() + r8eval.r8_suite():
            b = harness.chat_brief(s)
            self.assertEqual(bc.compress_brief(b, harness.BUDGET), r9.compress_brief(b, harness.BUDGET), s.name)

    def test_r10_set_prediction(self):
        # (a) same attribute + cue: superseded as r9; (e) different attribute + cue: kept as r4 (old in, new out)
        for s in r10eval.r10_suite():
            b = harness.chat_brief(s)
            out = bc.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(out), harness.BUDGET)
            p = {q.key: q for q in s.probes}
            for k in ("a1", "a2"):
                self.assertIn(p[k].current, out)
                self.assertNotIn(p[k].superseded[0], out)
            for k in ("d", "e1_old", "e2_old"):
                self.assertIn(p[k].current, out)
            for k in ("e1_new", "e2_new"):
                # budget-bound (pre-check): the newer different-attribute fact does not fit after the old ones
                self.assertNotIn(p[k].current, out)
                ex = next(e for e in exchanges(b) if p[k].current in e[0])
                self.assertGreater(len(out) + 1 + bc._size([ex]), harness.BUDGET)


if __name__ == "__main__":
    unittest.main()
