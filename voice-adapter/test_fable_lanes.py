import asyncio
import json
import os
import shutil
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
            merged = []

            async def merge(did, request, answers):
                merged.append(answers)
                return "Broker is up."
            call = SimpleNamespace(closed=False, profile="clean", counts=Counter(), trace=lambda *a, **k: None,
                                   lane_n={"d1": 2}, merge_lanes=merge,
                                   speak_for=lambda did, text, lane=False: said.append((did, text)))
            with mock.patch.object(Path, "home", return_value=Path(home)), \
                    mock.patch.object(mobile_live, "LANE_POLL", 0.05):
                task = asyncio.ensure_future(mobile_live.Call.fable_lanes(call, "d1", time.time() - 1, "is it up"))
                (root / "service.json").write_text(json.dumps({"lane": "service", "answer": "broker  up\non 8791"}))
                (root / "summary.txt").write_text("not a lane")
                (other / "relay.json").write_text(json.dumps({"lane": "relay", "answer": "other question"}))
                await asyncio.sleep(0.2)
                self.assertEqual(said, [])  # nothing spoken per lane: one merged update once the lanes land
                (root / "web.json").write_text(json.dumps({"lane": "web", "answer": "**Model guard:** hook chatter"}))
                (root / "files.json").write_text(json.dumps({"lane": "files", "answer": "port 8791 open"}))
                await asyncio.wait_for(task, 5)
        self.assertEqual(merged, [[("service", "broker up on 8791"), ("files", "port 8791 open")]])
        self.assertEqual(said, [("d1", "Broker is up.")])

    async def test_an_older_question_goes_quiet_once_a_newer_one_starts(self):
        """18:41 call: Q1's 45 s lane spoke over the owner's follow-up question."""
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".hermes/profiles/clean/cache/fast-lane/fable/voice-d1"
            root.mkdir(parents=True)
            said = []
            call = SimpleNamespace(closed=False, profile="clean", counts=Counter(), trace=lambda *a, **k: None,
                                   latest_did="d1", speak_for=lambda did, text, lane=False: said.append(text))
            with mock.patch.object(Path, "home", return_value=Path(home)):
                task = asyncio.ensure_future(mobile_live.Call.fable_lanes(call, "d1", time.time() - 1))
                await asyncio.sleep(0.1)
                call.latest_did = "d2"  # the owner asked something new
                (root / "history.json").write_text(json.dumps({"lane": "history", "answer": "late and off-topic"}))
                await asyncio.wait_for(task, 5)  # the speaker exits instead of waiting for the call to close
        self.assertEqual(said, [])


class OneVoice(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_instant_answer_is_spoken_per_question(self):
        said = []
        call = SimpleNamespace(closed=False, latest_did="d1", speak_for=lambda did, text, lane=False: said.append(text))
        say = lambda did, text: mobile_live.Call.say_first(call, did, text)
        self.assertFalse(say("d1", "SKIP"))
        self.assertFalse(say("d1", "**Model guard:** The hook flagged the active model"))
        self.assertTrue(say("d1", "Three sessions are running."))
        self.assertFalse(say("d1", "A second fast answer"))
        self.assertFalse(say("d0", "an older question's answer"))
        self.assertEqual(said, ["Three sessions are running."])

    def test_a_previous_turn_is_not_this_request(self):
        self.assertFalse(mobile_live.same_words(" Hey, what's up", "Awesome. Um, can you act as my chief of staff"))
        self.assertTrue(mobile_live.same_words(" Awesome. Um, so can you act as my chief of staff right now",
                                               "Awesome, so can you act as my chief of staff right now?"))

    async def test_a_timed_out_haiku_answer_is_not_given_to_the_next_question(self):
        haiku = mobile_live.WarmHaiku("sys")
        lines = asyncio.Queue()

        class Out:
            async def readline(self):
                return await lines.get()

        class In:
            def write(self, b): pass
            async def drain(self): pass
        haiku.proc = SimpleNamespace(returncode=None, stdin=In(), stdout=Out())
        with self.assertRaises(asyncio.TimeoutError):
            await haiku.ask("q1", 0.05)
        lines.put_nowait(json.dumps({"type": "result", "result": "answer to q1"}).encode())
        lines.put_nowait(json.dumps({"type": "result", "result": "answer to q2"}).encode())
        self.assertEqual(await haiku.ask("q2", 1), "answer to q2")

    @unittest.skipUnless(shutil.which("systemd-run"), "systemd hosts only; macOS caps children via launchd instead")
    def test_children_run_outside_the_adapters_memory_cap(self):
        self.assertEqual(mobile_live.SCOPE[:3], ["systemd-run", "--user", "--scope"])

    def test_live_sessions_lists_open_titled_sessions_newest_first(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as home:
            db = Path(home) / ".hermes/profiles/clean/state.db"
            db.parent.mkdir(parents=True)
            con = sqlite3.connect(db)
            con.executescript("create table sessions(id, title, last_activity_at, last_activity_description, ended_at);"
                              "create table messages(id integer primary key, session_id, role, content);")
            now = time.time()
            con.executemany("insert into sessions values(?,?,?,?,?)", [
                ("a", "IR remote", now - 60, "running", None), ("b", "Old", now - 9 * 3600, "", None),
                ("c", "Done", now - 30, "", now), ("d", "Evals", now - 600, "", None)])
            con.execute("insert into messages(session_id, role, content) values('a','assistant',?)",
                        ("Token registered as zer0-cast for the remote.",))
            con.commit(); con.close()
            with mock.patch.object(Path, "home", return_value=Path(home)):
                out = mobile_live.live_sessions()
        self.assertEqual([ln.split("] ")[1].split(" (")[0] for ln in out.splitlines()], ["IR remote", "Evals"])
        self.assertIn("zer0-cast", out)


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
