import asyncio
import json
import os
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mobile_live


class FableLanes(unittest.IsolatedAsyncioTestCase):
    async def test_each_new_lane_is_spoken_once_and_older_runs_never(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".hermes/profiles/clean/cache/fast-lane/fable/voice-d1"
            other = root.parent / "voice-d2"  # another open question's lanes must not be spoken here
            root.mkdir(parents=True)
            other.mkdir()
            old = root / "old.json"
            old.write_text(json.dumps({"lane": "old", "answer": "stale"}))
            os.utime(old, (1, 1))
            said = []
            call = SimpleNamespace(closed=False, profile="clean", counts=Counter(), trace=lambda *a, **k: None,
                                   speak_for=lambda did, text: said.append((did, text)))
            with mock.patch.object(Path, "home", return_value=Path(home)):
                task = asyncio.ensure_future(mobile_live.Call.fable_lanes(call, "d1", time.time() - 1))
                (root / "service.json").write_text(json.dumps({"lane": "service", "answer": "broker  up\non 8791"}))
                (root / "summary.txt").write_text("not a lane")
                (other / "relay.json").write_text(json.dumps({"lane": "relay", "answer": "other question"}))
                await asyncio.sleep(0.3)
                call.closed = True
                await asyncio.wait_for(task, 5)
        self.assertEqual(said, [("d1", "Helper lane service reports: broker up on 8791")])

    async def test_an_older_question_goes_quiet_once_a_newer_one_starts(self):
        """18:41 call: Q1's 45 s lane spoke over Kevin's follow-up "did you listen to anything I asked"."""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".hermes/profiles/clean/cache/fast-lane/fable/voice-d1"
            root.mkdir(parents=True)
            said = []
            call = SimpleNamespace(closed=False, profile="clean", counts=Counter(), trace=lambda *a, **k: None,
                                   latest_did="d1", speak_for=lambda did, text: said.append(text))
            with mock.patch.object(Path, "home", return_value=Path(home)):
                task = asyncio.ensure_future(mobile_live.Call.fable_lanes(call, "d1", time.time() - 1))
                await asyncio.sleep(0.1)
                call.latest_did = "d2"  # Kevin asked something new
                (root / "history.json").write_text(json.dumps({"lane": "history", "answer": "late and off-topic"}))
                await asyncio.wait_for(task, 5)  # the speaker exits instead of waiting for the call to close
        self.assertEqual(said, [])


class LaneAngles(unittest.TestCase):
    def test_lanes_answer_the_question_asked_not_z0live_health(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fable_lanes", mobile_live.FABLE_SCRIPT)
        fl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fl)
        pr = "Can you explain if any of our pull requests got merged in for Hermes? Who merged our work?"
        self.assertIs(fl.pick_angles(pr), fl.GENERAL)
        self.assertIn("github", list(fl.GENERAL)[:5])
        self.assertIs(fl.pick_angles("why did the voice call glitch out"), fl.Z0LIVE_ANGLES)
        self.assertIs(fl.pick_angles("what's the plan for the planet scene"), fl.GENERAL)  # "lane" inside a word


if __name__ == "__main__":
    unittest.main()
