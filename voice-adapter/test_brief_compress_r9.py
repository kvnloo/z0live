"""C1-ctx-compress-r9: explicit-update-cue supersession (the ONE change).

r7 (bench/brief_compress_r7.py, sha256 e22e84b6...) drops every older member of a shared "my <subject> is" slot.
r8 showed this false-merges two different true facts about one noun. r9 keeps r7's lexical slot, but a newer
same-slot member supersedes the older members ONLY when its own asker text carries an explicit update cue
(brief_compress.CUE, frozen in the r9 PREREG). Without a cue every member keeps r4's key, so the order and output are
byte-identical to r4 (bench/brief_compress_r4.py, sha256 5a88bc8b...).
"""
import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_r4 as r4  # noqa: E402
import brief_compress_r7 as r7  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
import ctx_compress_r6_eval as r6eval  # noqa: E402
import ctx_compress_r8_eval as r8eval  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs  # noqa: E402

R4_SHA256 = "5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64"
R7_SHA256 = "e22e84b64c76db340eccc98414000e8140872565b80f189e61869996a0f10b22"
CUES = ("now", "changed", "change", "instead", "new", "no longer", "not the old", "update", "updated", "moved",
        "switched", "replaced")
PAD = "x" * 60


def toy(update):
    # old value (shorter) first, two note facts, then a newer member of the same slot (longer)
    return "\n".join(["H",
                      "User: My tea blend is Rose-21. " + PAD, "Assistant: ok",
                      "User: note one " + PAD + "y" * 18, "Assistant: ok",
                      "User: note two " + PAD + "y" * 18, "Assistant: ok",
                      "User: " + update + " " + PAD[:50], "Assistant: ok"])


def exchanges(text):
    return brief_compress._split(text.split("\n"))[1]


def sweep(text):
    return range(1, len(text) + 2)  # every budget up to and past the full text


class Cue(unittest.TestCase):
    def test_frozen_cue_list(self):
        for c in CUES:
            self.assertTrue(brief_compress._cue([f"User: well, {c} it is Oak-1."]), c)
            self.assertTrue(brief_compress._cue([f"User: {c.upper()}: it is Oak-1."]), c)

    def test_word_boundaries(self):
        for line in ("User: my tea blend is Rose-21, I renew it yearly.", "User: nowhere else, newts, knowledge.",
                     "User: the exchange window and updates-feed are unrelated.", "User: my tea blend is Rose-21."):
            self.assertFalse(brief_compress._cue([line, "Assistant: ok"]), line)

    def test_reply_and_label_ignored(self):
        # only the asker's own text after the speaker label counts; a cue in the reply does not
        self.assertFalse(brief_compress._cue(["User: my tea blend is Rose-21.", "Assistant: noted, it is new now"]))
        self.assertFalse(brief_compress._cue(["New: my tea blend is Rose-21."]))
        self.assertTrue(brief_compress._cue(["New: my tea blend is now Rose-21."]))


class Supersede(unittest.TestCase):
    def test_cue_present_supersedes(self):
        text = toy("Changed: my tea blend is now Sage-48.")
        sizes = [brief_compress._size([e]) for e in exchanges(text)]
        budget = len("H") + 1 + sizes[3] + 1 + sizes[1]     # room for the new value and one note only
        out = brief_compress.compress_brief(text, budget)
        self.assertIn("Sage-48", out)
        self.assertNotIn("Rose-21", out)
        self.assertEqual(out, r7.compress_brief(text, budget))  # with a cue r9 acts as r7
        self.assertIn("Rose-21", r4.compress_brief(text, budget))  # red on r4

    def test_no_cue_keeps_both_in_r4_order(self):
        text = toy("My tea blend is tin Sage-48.")  # a second true fact about the tea blend, no update cue
        ex = exchanges(text)
        self.assertEqual(brief_compress._slot(ex[0]), brief_compress._slot(ex[3]))  # shared lexical slot
        idx = list(range(len(ex)))
        self.assertEqual(brief_compress._fact_order(ex, idx),
                         sorted(reversed(idx), key=lambda i: brief_compress._size([ex[i]])))
        self.assertNotEqual(r7._fact_order(ex, idx), brief_compress._fact_order(ex, idx))  # red on r7
        for b in sweep(text):
            self.assertEqual(brief_compress.compress_brief(text, b), r4.compress_brief(text, b), b)
        roomy = brief_compress._size([["H"], ex[0], ex[3]])  # room for both members, no note
        self.assertIn("Rose-21", brief_compress.compress_brief(text, roomy))  # both true facts kept
        self.assertIn("Sage-48", brief_compress.compress_brief(text, roomy))
        self.assertNotIn("Rose-21", r7.compress_brief(text, roomy))           # r7 dropped the older one

    def test_false_merge_pair_kept(self):
        ex = [["User: My bike is the red tourer tagged Owl-36."], ["User: note " + "n" * 40],
              ["User: My bike is booked in at the workshop this week, the job card says Crane-63."]]
        self.assertEqual(brief_compress._slot(ex[0]), brief_compress._slot(ex[2]))
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2]), [0, 1, 2])  # r4: shortest first
        self.assertEqual(r7._fact_order(ex, [0, 1, 2]), [2, 1])                  # r7 dropped the older fact

    def test_three_members_cue_in_middle(self):
        ex = [["User: my locker code is Ash-1."], ["User: my locker code is now Ash-22."],
              ["User: my locker code is Ash-333 for the spare."]]
        order = brief_compress._fact_order(ex, [0, 1, 2])
        self.assertEqual(sorted(order), [1, 2])   # the cue member drops the oldest; the cue-less newest drops nothing
        self.assertEqual(order[0], 1)             # the superseder takes the shortest size of the members it replaces

    def test_newest_cue_wins_over_older_cue(self):
        ex = [["User: my locker code is Ash-1."], ["User: my locker code is now Ash-22."],
              ["User: my locker code is now Ash-333."]]
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2]), [2])
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2]), r7._fact_order(ex, [0, 1, 2]))

    def test_cue_without_shared_slot_is_r4(self):
        ex = [["User: My cup is Oak-1. " + "b" * 20], ["User: My plate is now Elm-2."], ["User: c" * 3]]
        self.assertEqual(brief_compress._fact_order(ex, [0, 1, 2]),
                         sorted(reversed(range(3)), key=lambda i: brief_compress._size([ex[i]])))


class Differential(unittest.TestCase):
    def test_bench_copies(self):
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r4.py").read_bytes()).hexdigest(), R4_SHA256)
        self.assertEqual(hashlib.sha256((HERE / "bench" / "brief_compress_r7.py").read_bytes()).hexdigest(), R7_SHA256)

    def test_r4_byte_identical_on_capitalised_briefs(self):
        # FULL, OTHER, slm-v2-e5, slm-v2-live and the context_eval briefs, at every r2 budget
        for text in capitalised_briefs():
            for b in BUDGETS:
                self.assertEqual(brief_compress.compress_brief(text, b), r4.compress_brief(text, b), b)

    def test_main_set_identical_to_r4(self):
        for s in harness.suite():
            b = harness.chat_brief(s)
            self.assertEqual(brief_compress.compress_brief(b, harness.BUDGET), r4.compress_brief(b, harness.BUDGET))

    def test_recency_set_identical_to_r7(self):
        # r6 recency set (reused unchanged by r7): every newer same-slot fact says "Update: ... now ..., not the old"
        for s in r6eval.recency_suite():
            b = harness.chat_brief(s)
            self.assertEqual(brief_compress.compress_brief(b, harness.BUDGET), r7.compress_brief(b, harness.BUDGET))
            self.assertNotEqual(brief_compress.compress_brief(b, harness.BUDGET), r4.compress_brief(b, harness.BUDGET))

    def test_r8_set_prediction(self):
        # (a) newer members carry "now": superseded as in r7; (c) newer members carry no cue: both kept as in r4
        for s in r8eval.r8_suite():
            b = harness.chat_brief(s)
            out = brief_compress.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(out), harness.BUDGET)
            p = {q.key: q for q in s.probes}
            for k in ("a1", "a2"):
                self.assertIn(p[k].current, out)
                self.assertNotIn(p[k].superseded[0], out)
            for k in ("b1", "b2"):
                self.assertIn(p[k].superseded[0], out)
                self.assertNotIn(p[k].current, out)
            self.assertIn(p["c_old"].current, out)
            self.assertNotIn(p["c_new"].current, out)
            self.assertIn(p["d"].current, out)


if __name__ == "__main__":
    unittest.main()
