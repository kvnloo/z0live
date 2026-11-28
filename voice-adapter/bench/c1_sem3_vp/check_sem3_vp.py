"""Checks for the opt-in value-preserving trim (synthetic brief only). Run from this directory:
    python3 -m unittest -v check_sem3_vp
"""
import unittest

import brief_compress as bc
import brief_compress_sem2 as sem2
import brief_compress_sem3 as sem3
import sem3_vp

BUDGET = 2810
NOISE = ("note {i:02d}: the {w} sample has a subtle texture. We can compare it with the other samples after the "
         "display is assembled. No need to make a decision about that yet.")


def brief(old, new):
    noise = [f"{'user' if i % 2 == 0 else 'assistant'}: Weaving loft " + NOISE.format(i=i, w='shuttle') for i in range(16)]
    return "\n".join(["Context"] + [f"user: {t}" for t in old] + noise
                     + [ln for t in new for ln in (f"user: {t}", "assistant: Noted.")])


def warm_cache(text):
    """A fully cached warm state: statements about the dentist share one key, everything else is unique."""
    def key(s):
        return "dentist" if "dentist" in s else "u" + sem2.stmt_hash(s)[:8]
    cache = sem2.KeyCache(key1=key, judge=lambda *_: "replace")
    for s in sem2.statements(text, BUDGET):
        cache.add(s)
    return cache


class ValuePreservingTrim(unittest.TestCase):
    OLD = ["Lucian: My dentist appointment is on Monday at 8am.", "My drone tag is Harbour-41.",
           "I store my canoe at Bruges, row 3."]
    NEW = ["Bjorn: Actually, my dentist appointment on Monday is now at 2pm."]

    def setUp(self):
        self.text = brief(self.OLD, self.NEW)
        self.assertGreater(len(self.text), BUDGET)

    def test_default_flag_is_sem3_byte_for_byte(self):
        a = sem3.compress_brief_sem3_info(self.text, BUDGET, warm_cache(self.text))[0]
        b = sem3_vp.compress_brief_sem3_vp_info(self.text, BUDGET, warm_cache(self.text))[0]
        self.assertEqual(a, b)

    def test_unmodified_trim_drops_the_name_label_and_flag_keeps_it(self):
        plain = sem3.compress_brief_sem3_info(self.text, BUDGET, warm_cache(self.text))
        self.assertTrue(plain[1]["trimmed"], "fixture must exercise the trim")
        self.assertNotIn("Bjorn", plain[0])   # the defect under test
        out, info = sem3_vp.compress_brief_sem3_vp_info(self.text, BUDGET, warm_cache(self.text), value_preserving=True)
        self.assertIn("Bjorn:", out)
        self.assertLessEqual(len(out), BUDGET)
        self.assertIn("Harbour-41", out)
        self.assertIn("Bruges", out)

    def test_flag_restores_the_module_trim(self):
        before = sem3.trim_exchange
        sem3_vp.compress_brief_sem3_vp_info(self.text, BUDGET, warm_cache(self.text), value_preserving=True)
        self.assertIs(sem3.trim_exchange, before)

    def test_only_filler_is_dropped(self):
        stripped, removed = sem3_vp.strip_filler("Bjorn: Honestly, by the way my dentist appointment is now at 2pm.")
        self.assertEqual(stripped, "Bjorn: my dentist appointment is at 2pm.")
        self.assertTrue(sem3_vp.only_filler_dropped("Bjorn: Honestly, by the way my dentist appointment is now at 2pm.",
                                                    stripped, removed))
        # a name before the digit clause is value-bearing: never filler
        self.assertFalse(sem3_vp.only_filler_dropped("Moved my canoe to Riga, bay 4.", "bay 4."))

    def test_no_fit_keeps_the_line_whole(self):
        long_new = "Bjorn: Change of plan, the dentist appointment was moved a few times and it is now Monday at 2pm."
        text = brief(self.OLD, [long_new])
        out, info = sem3_vp.compress_brief_sem3_vp_info(text, BUDGET, warm_cache(text), value_preserving=True)
        self.assertIn("Bjorn", out)


if __name__ == "__main__":
    unittest.main()
