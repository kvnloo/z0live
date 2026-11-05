"""z0live.lanes.v1 on the broker: config + hot reload, blackboard, fan-out splice, text/voice config."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import lanes, lanes_intent, lanes_v1  # noqa: E402
from z0live_desktop.blackboard import Blackboard, Stale  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.lane_fanout import Fanout  # noqa: E402

CARD_SHA = "1522764dfd76b8e83d8664125d4701e823db1996ccb0b18302d31f46f3ec76e2"  # aodl-views 837a714 lanes.py
PLAN = {"meta_role": "plan", "roles": {"smol": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                                       "plan": {"provider": "anthropic", "model": "claude-opus-5-5"},
                                       "task": {"provider": "openai-codex", "model": "gpt-6.1-sol"}}}


class FakeModels:
    """Scripted lane replies by model name: (delay_s, text) or an exception."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def complete(self, binding, prompt, turn="lane", timeout=20):
        b = lanes.resolve(binding, PLAN)
        self.calls.append((b["model"], prompt))
        delay, reply = self.replies.get(b["model"], (0, RuntimeError("down")))
        time.sleep(delay)
        if isinstance(reply, Exception):
            raise reply
        return {"text": reply, "model": b["model"], "ms": int(delay * 1000)}


class Base(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "z0live-lanes.json"
        env = patch.dict(os.environ, {"Z0LIVE_LANES": str(self.cfg_path)})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.tmp.name) / "broker")
        self.device = self.store.authenticate(self.store.pair("unit"))
        self.sid = self.store.new_session(self.device, {"client": "web", "context": {}, "tools": []})["session_id"]


class ConfigTests(Base):
    def test_card_validator_is_verbatim(self):
        self.assertEqual(hashlib.sha256(Path(lanes_v1.__file__).read_bytes()).hexdigest(), CARD_SHA)

    def test_default_is_the_spoken_example_and_valid(self):
        self.assertEqual(lanes.validate(lanes.DEFAULT), [])
        self.assertEqual(lanes_intent.parse(lanes.EXAMPLE)["lanes"], lanes.DEFAULT["lanes"])
        self.assertEqual(lanes_intent.counts(lanes.DEFAULT), {"fast": 2, "small": 2, "large": 2, "council": 1})

    def test_edits_rebind_one_tier_and_counts_reshape(self):
        cfg = lanes_intent.parse("make the large lanes Opus and Sonnet")
        self.assertEqual([b["model"] for b in lanes.large(cfg)], ["claude-opus-5-5", "claude-sonnet-5"])
        self.assertEqual(lanes.tier(cfg, "fast"), lanes.tier(lanes.DEFAULT, "fast"))
        cfg = lanes_intent.parse("gimme one fast lane, three small, 1 large and no council", cfg)
        self.assertEqual(lanes_intent.counts(cfg), {"fast": 1, "small": 3, "large": 1, "council": 0})
        self.assertEqual(lanes.validate(cfg), [])
        with self.assertRaises(ValueError):
            lanes_intent.parse("gimme 6 fast lanes")  # pool has 3
        with self.assertRaises(ValueError):
            lanes_intent.parse("what's the weather")

    def test_roles_resolve_from_voice_plan_and_gpt_roles_are_refused(self):
        self.assertEqual(lanes.resolve({"provider": "voice_plan", "model": "smol", "route": "voice_plan"}, PLAN)["model"],
                         "claude-haiku-4-5")
        with self.assertRaisesRegex(ValueError, "fx/pi"):
            lanes.resolve({"provider": "voice_plan", "model": "task", "route": "voice_plan"}, PLAN)
        bad = json.loads(json.dumps(lanes.DEFAULT))
        bad["lanes"]["driver"]["verifiers"] = [{"provider": "openai", "model": "gpt-6.1-sol", "route": "hermes"}]
        self.assertTrue(lanes.validate(bad))

    def test_hot_reload_keeps_last_good_on_bad_edit(self):
        live = lanes.Lanes()
        self.assertEqual(live.get()["revision"], lanes.DEFAULT["revision"])
        self.assertEqual(live.status()["source"], "default")
        lanes_intent.apply("2 fast, 1 small, 1 large, no council")
        self.assertEqual(lanes_intent.counts(live.get()), {"fast": 2, "small": 1, "large": 1, "council": 0})
        good = live.get()["revision"]
        self.cfg_path.write_text('{"schema": "z0live.lanes.v1", "lanes": {}}')
        self.assertEqual(live.get()["revision"], good)
        self.assertTrue(live.status()["issues"])


class BlackboardTests(Base):
    def test_versions_roles_and_epochs(self):
        board = Blackboard(self.store)
        e = board.write(self.sid, "small:qwen", "small", "Paris.", 0.6, 0)
        with self.assertRaises(PermissionError):
            board.verify(e, "small:qwen", "small")
        with self.assertRaises(PermissionError):
            board.write(self.sid, "small:qwen", "small", "x", 1, 0, status="verified")
        new = board.correct(e, "large:opus", "large", "Paris, France.", 0.9)
        view = board.read(self.sid)
        self.assertEqual([(x["id"], x["status"], x["supersedes"]) for x in view["entries"]], [(new, "corrected", e)])
        self.assertEqual(board.entry(e)["superseded_by"], new)
        self.assertFalse(board.verify(e, "large:opus", "large"))  # superseded, not current
        self.assertEqual(board.bump(self.sid, "barge_in"), 1)
        self.assertEqual(board.read(self.sid)["entries"], [])
        with self.assertRaises(Stale):
            board.write(self.sid, "small:qwen", "small", "late", 0.5, 0)


class FanoutTests(Base):
    def fanout(self, replies, spoken):
        models = FakeModels(replies)
        out = Fanout(self.store, models=models, publish=lambda sid, say, src: spoken.append((say, src)))
        return out, models

    def test_race_writes_answer_and_intent_then_large_corrects_once(self):
        spoken = []
        out, models = self.fanout({
            "qwen3-0.6b-q8": (0, '{"route":"answer","intent":"capital of australia"}'),
            "qwen3-1.7b": (0.3, '{"route":"answer","intent":"x"}'),
            "claude-haiku-4-5": (0.05, '{"route":"answer","answer":"Sydney.","confidence":0.6}'),
            "qwen3.5-4b": (0.5, '{"route":"meta","answer":""}'),
            "claude-opus-5-5": (0.05, '{"verdict":"correct","answer":"Canberra is the capital.","confidence":0.95}'),
            "claude-sonnet-5": (0.2, '{"verdict":"correct","answer":"Canberra.","confidence":0.9}'),
        }, spoken)
        receipt = out.on_final(self.sid, "what's the capital of australia")
        self.assertEqual(receipt["answer"], "Sydney.")
        first = out.read(self.sid)  # the voice hears the draft
        self.assertEqual([(e["role"], e["kind"], e["status"]) for e in first["entries"]],
                         [("fast", "intent", "draft"), ("small", "answer", "draft")])
        receipt["verify"].result(timeout=5)
        view = out.read(self.sid)["entries"]
        self.assertEqual(view[-1]["status"], "corrected")
        self.assertEqual(view[-1]["text"], "Canberra is the capital.")
        self.assertEqual(spoken, [("Actually, Canberra is the capital.", f"lane-correction:{view[-1]['id']}")])
        # second turn: the one-correction budget is spent, nothing more is spoken
        receipt = out.on_final(self.sid, "and of new zealand")
        receipt["verify"].result(timeout=5)
        out.read(self.sid)
        self.assertEqual(len(spoken), 1)

    def test_agreeing_verifier_marks_verified_and_meta_is_no_answer(self):
        out, _ = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hi Kevin."}'),
                              "claude-opus-5-5": (0, '{"verdict":"agree","answer":""}'),
                              "claude-sonnet-5": (0, '{"verdict":"agree","answer":""}')}, [])
        receipt = out.on_final(self.sid, "hey")
        receipt["verify"].result(timeout=5)
        self.assertEqual(out.read(self.sid)["entries"][-1]["status"], "verified")
        out, _ = self.fanout({"claude-haiku-4-5": (0, '{"route":"meta","answer":""}')}, [])
        self.assertIsNone(out.on_final(self.sid, "did my build pass")["answer"])

    def test_barge_in_epoch_drops_a_late_lane(self):
        out, _ = self.fanout({"claude-haiku-4-5": (0.4, '{"route":"answer","answer":"Late."}')}, [])
        result = {}
        t = threading.Thread(target=lambda: result.update(out.on_final(self.sid, "tell me a joke")))
        t.start()
        time.sleep(0.1)
        out.cancel(self.sid, "barge_in")
        t.join(5)
        self.assertEqual(result.get("dropped"), "stale epoch")
        self.assertEqual(out.read(self.sid)["entries"], [])

    def test_deadline_bounds_the_race(self):
        out, _ = self.fanout({"claude-haiku-4-5": (2.0, '{"route":"answer","answer":"Slow."}')}, [])
        start = time.monotonic()
        self.assertIsNone(out.on_final(self.sid, "hello")["answer"])
        self.assertLess(time.monotonic() - start, 1.9)  # smol.deadline_ms = 1200


class BrokerWiringTests(Base):
    def test_transcript_fans_out_and_voice_tools_read_and_configure(self):
        env = patch.dict(os.environ, {"Z0LIVE_LANES_FANOUT": "on"})
        env.start()
        self.addCleanup(env.stop)
        models = FakeModels({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hello."}'),
                             "claude-opus-5-5": (0, '{"verdict":"agree"}')})
        self.store.lanes.models = models
        self.store.add_events(self.device, [{"session_id": self.sid, "type": "user_transcript", "text": "hello there"}])
        for _ in range(50):
            entries = self.store.tool(self.device, "read_context", {"session_id": self.sid, "arguments": {}})["entries"]
            if any(e["kind"] == "answer" for e in entries):
                break
            time.sleep(0.05)
        self.assertEqual([e["text"] for e in entries if e["kind"] == "answer"], ["Hello."])
        result = self.store.tool(self.device, "configure_lanes",
                                 {"session_id": self.sid, "arguments": {"request": "make the large lanes opus and sonnet"}})
        self.assertTrue(result["ok"], result)
        self.assertIn("claude-opus-5-5, claude-sonnet-5", result["say"])
        self.assertEqual(json.loads(self.cfg_path.read_text())["revision"], result["revision"])
        refused = self.store.tool(self.device, "configure_lanes", {"session_id": self.sid, "arguments": {"request": "9 fast"}})
        self.assertFalse(refused["ok"])
        self.store.add_events(self.device, [{"session_id": self.sid, "type": "barge_in"}])
        self.assertEqual(self.store.tool(self.device, "read_context", {"session_id": self.sid, "arguments": {}})["entries"], [])


if __name__ == "__main__":
    unittest.main()
