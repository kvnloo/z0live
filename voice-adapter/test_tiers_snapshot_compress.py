import unittest

import tiers


class SnapshotCompress(unittest.TestCase):
    def test_short_brief_unchanged(self):
        self.assertIn("Context from his Hermes chat:\nA: hi\nB: yo\n\n", tiers.snapshot("A: hi\nB: yo", []))

    def test_live_history_keeps_updated_and_distinct_facts_with_other_context(self):
        header = "Recent chat context (do not read aloud):"
        brief = "\n".join([
            header,
            "user: My kayak is on the north pier, the berth tag is Oak-1.",
            "assistant: Got it.",
            "user: My kayak is booked for a lesson, the lesson ref is Pine-3.",
            "assistant: Got it.",
            "user: My kayak is now on the south pier, the berth tag is Elm-2.",
            "assistant: Got it.",
        ] + ["user: Explain a general topic.\nassistant: " + "Long reply. " * 45 +
             "\nNote: additional explanation\nExample: another detail"] * 12)
        out = tiers.snapshot(brief, [("user", "Which berth?")], live="Live lanes", ledger="Job receipt")
        self.assertIn(header, out)
        self.assertIn("Elm-2", out)
        self.assertIn("Pine-3", out)
        self.assertNotIn("Oak-1", out)
        self.assertIn("Live lanes", out)
        self.assertIn("Job receipt", out)
        self.assertIn("User: Which berth?", out)

    def test_long_brief_is_capped_and_keeps_header(self):
        lines = ["Header line"]
        for i in range(120):
            lines += [f"Asker: question number {i} " + "x" * 40, f"Bot: " + "reply " * 30 + f"end{i}"]
        out = tiers.snapshot("\n".join(lines), [("user", "now")])
        body = out.split("Context from his Hermes chat:\n")[1].split("\n\nThis call so far")[0]
        self.assertLessEqual(len(body), tiers.BRIEF_BUDGET)
        # r4+ (C56-live-compress-r4) deliberately stops force-keeping the newest exchange; the live call turns follow.
        self.assertIn("Header line", body)
        self.assertIn("User: now", out)


if __name__ == "__main__":
    unittest.main()
