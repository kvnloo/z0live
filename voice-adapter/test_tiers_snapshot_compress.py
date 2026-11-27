import unittest

import tiers


class SnapshotCompress(unittest.TestCase):
    def test_short_brief_unchanged(self):
        self.assertIn("Context from his Hermes chat:\nA: hi\nB: yo\n\n", tiers.snapshot("A: hi\nB: yo", []))

    def test_long_brief_is_capped_and_keeps_header(self):
        lines = ["Header line"]
        for i in range(120):
            lines += [f"Asker: question number {i} " + "x" * 40, f"Bot: " + "reply " * 30 + f"end{i}"]
        out = tiers.snapshot("\n".join(lines), [("user", "now")])
        body = out.split("Context from his Hermes chat:\n")[1].split("\n\nThis call so far")[0]
        self.assertLessEqual(len(body), tiers.BRIEF_BUDGET)
        # r4+ (C56-live-compress-r4) deliberately stops force-keeping the newest exchange; the live call turns follow.
        self.assertIn("Header line", body)
        self.assertIn("Kevin: now", out)


if __name__ == "__main__":
    unittest.main()
