import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from z0live_desktop.attached_lanes import small_answer
from z0live_desktop.core import Store
from test_voice_stack import plan


class AttachedLanesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.target = {"profile": "clean", "session": "exact-session"}

    def answer(self, result, selected=None):
        calls = []
        def small(*args):
            calls.append(args)
            return result
        with patch("z0live_desktop.voice_runtime.Journal.plan", return_value=selected or plan()), \
             patch("z0live_desktop.attached_lanes.conversation", return_value=[{"id": 7, "role": "user", "text": "Fix voice"}]):
            value = small_answer(self.store, "call", "item", self.target, "Hi", "Kevin: hello", small)
        return value, calls

    def test_smol_uses_pinned_binding_and_both_conversations(self):
        (answer, marks), calls = self.answer({"model": "unit-haiku", "ms": 102, "text": json.dumps({"route": "answer", "answer": "Hello."})})
        self.assertEqual(answer, "Hello.")
        self.assertEqual(calls[0][1], plan()["roles"]["smol"])
        self.assertIn('"id": 7', calls[0][2])
        self.assertIn("Kevin: hello", calls[0][2])
        self.assertEqual(marks["meta_role"], "plan")
        self.assertEqual(marks["smol_ms"], 102)

    def test_meta_and_invalid_results_cannot_be_spoken_as_work_done(self):
        for result in [
            {"model": "unit-haiku", "text": '{"route":"meta","answer":""}'},
            {"model": "wrong-model", "text": '{"route":"answer","answer":"Done"}'},
            {"model": "unit-haiku", "text": "not JSON"},
        ]:
            (answer, marks), calls = self.answer(result)
            self.assertIsNone(answer)
            self.assertEqual(len(calls), 1)

    def test_missing_plan_never_calls_a_fallback_model(self):
        with patch("z0live_desktop.voice_runtime.Journal.plan", return_value=None):
            answer, observed = small_answer(self.store, "call", "item", self.target, "Hello", "",
                                             lambda *args: self.fail("unexpected inference"))
        self.assertIsNone(answer)
        self.assertIn("unavailable", observed["smol"])


if __name__ == "__main__":
    unittest.main()
