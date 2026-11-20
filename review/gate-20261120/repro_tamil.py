"""Review-gate repro for feat/tamil (2a3b1ff). Run from voice-adapter/ of the head under test (see repro_guest_floor.py).

English `no` counts as a correction only before a comma (`no(?= *,)`). The Tamil negation has no such guard, so an
ordinary negative sentence is labelled a correction. Label precision should not depend on the caller's language.
"""
from test_call_trace import CallTraceTests


class TamilPrecision(CallTraceTests):
    async def test_plain_negative_sentence_is_not_a_correction(self):
        call = await self.make_call()
        cases = (
            ("I have no time today", False),        # English control: passes today
            ("எனக்கு நேரம் இல்லை", False),            # "I have no time": plain negation
            ("enakku neram illa", False),            # same, romanized
            ("இல்லை, use blue", True),               # turn-initial "no," stays a correction
        )
        for text, _ in cases:
            self.user(call, text)
        rows = self.rows("user.reaction")
        for (text, correction), row in zip(cases, rows):
            with self.subTest(text=text):
                self.assertEqual(row["correction"], correction)


def load_tests(loader, tests, pattern):  # run only the repro, not the inherited suite
    return loader.loadTestsFromName("test_plain_negative_sentence_is_not_a_correction", TamilPrecision)
