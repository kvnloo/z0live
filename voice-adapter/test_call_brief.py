import os
import tempfile
import time
import unittest
from pathlib import Path

import call_brief


def msg(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


class Brief(unittest.TestCase):
    def test_digest_is_framed_background_not_conversation(self):
        hist = [msg("user", "pls monitor thru adb"), msg("assistant", "Watching now. Details follow.")]
        with tempfile.TemporaryDirectory() as d:
            call_brief.BRIEF_DIR = Path(d)
            text = call_brief.build(hist)
        self.assertIn("Do not reply to it", text)
        self.assertIn("pls monitor thru adb", text)
        self.assertIn("approved it watching his screen", text)
        self.assertNotIn("assistant:", text)  # no transcript roles the model could continue

    def test_written_brief_wins_while_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            call_brief.save("Kevin is testing the z0live switch.", "", root)
            self.assertIn("z0live switch", call_brief.written("", root=root))
            old = time.time() - call_brief.FRESH_S - 10
            os.utime(root / "latest.md", (old, old))
            self.assertEqual(call_brief.written("", root=root), "")

    def test_chat_brief_beats_global(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            call_brief.save("global", "", root)
            call_brief.save("this chat", "c1", root)
            self.assertEqual(call_brief.written("c1", root=root), "this chat")


if __name__ == "__main__":
    unittest.main()
