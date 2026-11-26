"""C1-ctx-compress-r3: a reply-less user statement is a fact, not filler (the ONE change).

bench/brief_compress_r2.py is the frozen r2 compress_brief (byte copy of brief_compress.py at e43c29f,
sha256 f5478ee3...). Changed fixtures, exactly: the three context_eval briefs (capitalised and lowercase labels),
which hold reply-less user lines; FULL, OTHER, slm-v2-e5 and slm-v2-live hold none and stay byte-identical.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_r2 as r2  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as harness  # noqa: E402
from test_brief_compress_r2 import BUDGETS, capitalised_briefs, lower_brief  # noqa: E402

FILLER = ("Assistant: " + "That is a long general explanation of how sample textures compare. " * 4).strip()


def replyless(text):
    parts = brief_compress._split(text.split("\n"))
    return parts is not None and any(len(ex) == 1 for ex in parts[1])


def toy():
    # oldest user statement gets no reply; the rest is filler pairs (> FACT_REPLY_CHARS replies)
    lines = ["Synthetic header line", "User: My locker code is Fern-12."]
    for i in range(8):
        lines += [f"User: tell me about sample {i}", FILLER]
    return "\n".join(lines)


class ReplylessUserStatementIsFact(unittest.TestCase):
    def test_replyless_statement_kept(self):
        text = toy()
        out = brief_compress.compress_brief(text, 640)
        self.assertLessEqual(len(out), 640)
        self.assertIn("User: My locker code is Fern-12.", out.split("\n"))
        self.assertNotIn("Fern-12", r2.compress_brief(text, 640))  # r2 filed it as filler and dropped it

    def test_r2_harness_dry_prediction(self):
        # PREREG dry-run prediction (recorded, not a goal): at 2810 the reply-less turns compete as facts newest
        # first with context_eval's noise pairs (~189-char replies, also facts), so the two newest reply-less turns
        # are kept and the two oldest (vendor, flight-old) still do not fit; r2 kept flight-old as filler instead.
        for s in harness.suite():
            b = harness.chat_brief(s)
            out = brief_compress.compress_brief(b, harness.BUDGET)
            self.assertLessEqual(len(out), harness.BUDGET)
            self.assertTrue(out.startswith(s.stable + "\n"))
            ex = brief_compress._split(b.split("\n"))[1]
            lines = out.split("\n")
            kept = {i for i, x in enumerate(ex) if x[0] in lines}
            kept_r2 = {i for i, x in enumerate(ex) if x[0] in r2.compress_brief(b, harness.BUDGET).split("\n")}
            single = [i for i, x in enumerate(ex) if len(x) == 1]
            self.assertEqual(single[:4], [0, 1, 5, 6])
            self.assertEqual(kept & {0, 1, 5, 6}, {5, 6})
            self.assertEqual(kept_r2 & {0, 1, 5, 6}, {1, 6})
            vendor = [p.current for p in s.probes if p.key == "vendor"][0]
            self.assertNotIn(vendor, out)

    def test_byte_identical_to_r2_without_replyless_lines(self):
        checked = 0
        for text in capitalised_briefs():
            if replyless(text):
                continue
            checked += 1
            for b in BUDGETS:
                self.assertEqual(brief_compress.compress_brief(text, b), r2.compress_brief(text, b))
        self.assertEqual(checked, 4)  # FULL, OTHER, slm-v2-e5, slm-v2-live

    def test_changed_fixtures_are_exactly_context_eval(self):
        flagged = [replyless(t) for t in capitalised_briefs()]
        self.assertEqual(flagged, [False] * 4 + [True] * len(context_eval.suite()))

    def test_lowercase_equals_capitalised_with_labels_lowered(self):
        for s in context_eval.suite():
            cap = s.stable + "\n" + "\n".join(f"{r.capitalize()}: {t}" for r, t in context_eval.exchange(s.chat))
            for b in BUDGETS:
                want = brief_compress.compress_brief(cap, b)
                if b == 0 or (len(cap) > b and want == cap[-b:]):
                    continue
                lowered = "\n".join(ln.replace("User: ", "user: ", 1).replace("Assistant: ", "assistant: ", 1)
                                    for ln in want.split("\n"))
                self.assertEqual(brief_compress.compress_brief(lower_brief(s), b), lowered)

    def test_verbatim_in_order(self):
        for s in harness.suite():
            text = harness.chat_brief(s)
            out = brief_compress.compress_brief(text, harness.BUDGET).split("\n")
            self.assertEqual([ln for ln in text.split("\n") if ln in out], out)


if __name__ == "__main__":
    unittest.main()
