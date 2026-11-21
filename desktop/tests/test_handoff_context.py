import json
import tempfile
import time
import unittest
from pathlib import Path

from z0live_desktop.live import handoff_context, persona


class HandoffContext(unittest.TestCase):
    def write(self, d, **kw):
        path = Path(d) / "handoff.json"
        data = {"at": time.time(), "chat": "c1", "exchanges": [["user", "what sessions are running"], ["assistant", "three"]]}
        data.update(kw)
        path.write_text(json.dumps(data))
        return path

    def test_fresh_handoff_lands_in_persona_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d)
            ctx = handoff_context(path)
            self.assertIn("Kevin: what sessions are running", ctx)
            self.assertIn("do not greet", ctx)
            self.assertIn("what sessions are running", persona([], ctx))
            self.assertFalse(path.exists())
            self.assertEqual(handoff_context(path), "")

    def test_stale_or_missing_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(handoff_context(self.write(d, at=time.time() - 900)), "")
            self.assertEqual(handoff_context(Path(d) / "none.json"), "")


if __name__ == "__main__":
    unittest.main()
