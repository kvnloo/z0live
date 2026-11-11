"""Lane status for live-mode UIs: real per-lane state from recorded lane jobs, blackboard and health."""

import copy
import threading
import time
import unittest
from unittest.mock import patch

from test_lanes_broker import Base, FakeModels  # noqa: F401 - shared fixtures

from z0live_desktop import lanes, lane_status
from z0live_desktop.lane_fanout import Fanout


class LaneStatusTests(Base):
    def fanout(self, replies, preset="D"):
        out = Fanout(self.store, models=FakeModels(replies), publish=lambda *a: None)
        out.calls[self.sid] = copy.deepcopy(lanes.PRESETS[preset])
        lane_status.install(out)
        return out

    def rows(self, out):
        with patch.object(lane_status, "_probe", side_effect=lambda base: "URLError" if "15637" in base else None):
            return {r["id"]: r for r in lane_status.status(out, self.sid)["lanes"]}

    def test_idle_call_invents_nothing_and_dead_local_lane_shows_dead(self):
        out = self.fanout({})
        rows = self.rows(out)
        self.assertEqual(set(rows), {"fast:qwen3-0.6b-q8", "fast:qwen3-1.7b", "small:smol", "large:slow"})
        self.assertEqual(rows["fast:qwen3-0.6b-q8"]["state"], "dead")
        self.assertEqual(rows["fast:qwen3-0.6b-q8"]["error"], "URLError")
        for key in ("fast:qwen3-1.7b", "small:smol", "large:slow"):
            self.assertEqual((rows[key]["state"], rows[key]["researching"], rows[key]["entry"]), ("idle", None, None))

    def test_model_bound_twice_in_one_role_is_one_lane(self):
        out = self.fanout({})
        models = out.calls[self.sid]["lanes"]["smol"]["models"]
        models[1] = dict(models[0])  # the stored "router06b" variant: the 0.6b both routes and races
        ids = [r["id"] for r in lane_status.status(out, self.sid)["lanes"]]
        self.assertEqual(ids, ["fast:qwen3-0.6b-q8", "small:smol", "large:slow"])

    def test_answer_then_verify_and_errors(self):
        out = self.fanout({
            "qwen3-1.7b": (0, '{"route":"answer","intent":"capital","kind":"simple","complete":true}'),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Canberra.","confidence":0.7}'),
            "claude-opus-5-5": (0.05, '{"route":"answer","answer":"Canberra.","confidence":0.9}'),
        })
        receipt = out.on_final(self.sid, "what's the capital of australia")
        receipt["verify"].result(timeout=5)
        rows = self.rows(out)
        small = rows["small:smol"]
        self.assertEqual(small["resolved"], "claude-haiku-4-5")
        self.assertEqual(small["state"], "verified")
        self.assertEqual(small["researching"], "answer: what's the capital of australia")
        self.assertEqual((small["entry"]["text"], small["entry"]["confidence"]), ("Canberra.", 0.7))
        self.assertEqual(rows["large:slow"]["state"], "verified")
        self.assertEqual(rows["fast:qwen3-1.7b"]["state"], "answered")
        self.assertEqual(rows["fast:qwen3-1.7b"]["entry"]["kind"], "intent")

    def test_inflight_job_is_thinking_with_its_topic(self):
        gate = threading.Event()

        class Slow(FakeModels):
            def complete(self, binding, prompt, *a, **k):
                if lanes.resolve(binding, self.plan)["model"] == "claude-opus-5-5":
                    gate.wait(5)
                return super().complete(binding, prompt, *a, **k)

        out = Fanout(self.store, models=Slow({
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Four.","confidence":0.6}'),
            "claude-opus-5-5": (0, '{"route":"answer","answer":"Four.","confidence":0.9}')}), publish=lambda *a: None)
        out.calls[self.sid] = copy.deepcopy(lanes.PRESETS["D"])
        lane_status.install(out)
        receipt = out.on_final(self.sid, "two plus two")
        deadline = time.time() + 2
        while self.rows(out)["large:slow"]["state"] != "thinking" and time.time() < deadline:
            time.sleep(0.02)
        large = self.rows(out)["large:slow"]
        self.assertEqual((large["state"], large["researching"]), ("thinking", "answer: two plus two"))
        gate.set()
        receipt["verify"].result(timeout=5)
        self.assertEqual(self.rows(out)["large:slow"]["state"], "verified")

    def test_failed_job_is_dead_and_corrected_large_lane(self):
        out = self.fanout({
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Sydney.","confidence":0.6}'),
            "claude-opus-5-5": (0, '{"route":"answer","answer":"Canberra is the capital.","confidence":0.95}'),
            "qwen3-1.7b": (0, RuntimeError("down")),
        })
        receipt = out.on_final(self.sid, "what's the capital of australia")
        receipt["verify"].result(timeout=5)
        rows = self.rows(out)
        self.assertEqual((rows["fast:qwen3-1.7b"]["state"], rows["fast:qwen3-1.7b"]["error"]), ("dead", "RuntimeError"))
        self.assertEqual(rows["large:slow"]["state"], "corrected")
        self.assertEqual(rows["large:slow"]["entry"]["text"], "Canberra is the capital.")

    def test_store_route_defaults_to_newest_session(self):
        with patch.object(lane_status, "_probe", return_value=None), \
                patch.object(Fanout, "_models", lambda self, sid: FakeModels({})):
            view = self.store.lane_status(self.device)
        self.assertEqual(view["session_id"], self.sid)
        self.assertTrue(view["open"])
        self.assertTrue(view["lanes"])

    def test_abandoned_call_is_not_open(self):
        with self.store.connect() as db:  # never ended, nothing heard for an hour
            db.execute("UPDATE sessions SET created=created-3600, updated=updated-3600 WHERE id=?", (self.sid,))
        with patch.object(lane_status, "_probe", return_value=None), \
                patch.object(Fanout, "_models", lambda self, sid: FakeModels({})):
            view = self.store.lane_status(self.device)
        self.assertEqual(view["session_id"], self.sid)
        self.assertFalse(view["open"])


if __name__ == "__main__":
    unittest.main()
