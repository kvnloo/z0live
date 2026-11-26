"""C1-ctx-compress-r2: compress_brief detects speaker labels case-insensitively (the ONE change).

bench/brief_compress_c1.py is the frozen pre-r2 compress_brief (byte copy of brief_compress.py at 2ffa205).
"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bench"))
import brief_compress  # noqa: E402
import brief_compress_c1 as old  # noqa: E402
import context_eval  # noqa: E402
from test_brief_compress import FULL, OTHER  # noqa: E402

BUDGETS = (0, 1, 50, 200, 500, 1000, 1500, 2000, 2810, 3500, 5000, 10 ** 6)


def capitalised_briefs():
    out = [FULL, OTHER]
    for name in ("slm-v2-e5.json", "slm-v2-live.json"):
        out.append(json.loads((HERE / "bench" / name).read_text())["brief"])
    for s in context_eval.suite():  # context_eval's brief with capitalised labels
        out.append(s.stable + "\n" + "\n".join(f"{r.capitalize()}: {t}" for r, t in context_eval.exchange(s.chat)))
    return out


def lower_brief(s):
    return s.stable + "\n" + "\n".join(f"{r}: {t}" for r, t in context_eval.exchange(s.chat))


class CaseInsensitiveSpeakers(unittest.TestCase):
    def test_capitalised_path_byte_identical_to_c1(self):
        # differential: every brief the old compressor already parsed gives the same bytes at every budget
        for text in capitalised_briefs():
            for b in BUDGETS:
                self.assertEqual(brief_compress.compress_brief(text, b), old.compress_brief(text, b))

    def test_old_compressor_tail_cuts_lowercase_brief(self):
        # the r1 mechanism: no speaker structure found -> newest budget chars
        for s in context_eval.suite():
            text = lower_brief(s)
            self.assertEqual(old.compress_brief(text, 2810), text[-2810:])

    def test_lowercase_brief_takes_speaker_path(self):
        for s in context_eval.suite():
            text = lower_brief(s)
            out = brief_compress.compress_brief(text, 2810)
            self.assertNotEqual(out, text[-2810:])
            self.assertLessEqual(len(out), 2810)
            self.assertTrue(out.startswith(s.stable + "\n"))  # header (stable brief) kept
            lines = text.split("\n")
            self.assertEqual([ln for ln in lines if ln in out.split("\n")], out.split("\n"))  # verbatim, in order

    def test_lowercase_equals_capitalised_with_labels_lowered(self):
        for s in context_eval.suite():
            cap = s.stable + "\n" + "\n".join(f"{r.capitalize()}: {t}" for r, t in context_eval.exchange(s.chat))
            for b in BUDGETS:
                want = old.compress_brief(cap, b)
                if b == 0 or (len(cap) > b and want == cap[-b:]):
                    continue  # tail fallback cuts mid-line; label mapping not defined there
                lowered = "\n".join(ln.replace("User: ", "user: ", 1).replace("Assistant: ", "assistant: ", 1)
                                    for ln in want.split("\n"))
                self.assertEqual(brief_compress.compress_brief(lower_brief(s), b), lowered)

    def test_mixed_case_only_used_when_capitalised_finds_no_pair(self):
        # one capitalised speaker + lowercase lines: the capitalised parse still fails, so the case-insensitive
        # parse decides; a brief with two capitalised speakers never consults it
        text = "Header line\nKim: hi there\nnote: x\nAssistant: hello\n" + "Kim: filler " * 50
        self.assertEqual(brief_compress.compress_brief(text, 100), old.compress_brief(text, 100))


if __name__ == "__main__":
    unittest.main()
