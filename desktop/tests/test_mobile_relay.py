"""Hermes-app Live relay over the real broker HTTP routes (/v1/lanes/turn, read_context), scripted lanes."""

import asyncio
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lanes_broker import Base, FakeModels  # noqa: E402
from z0live_desktop import server  # noqa: E402
from z0live_desktop.mobile_relay import LaneRelay, covered  # noqa: E402


class RelayTests(Base):
    def setUp(self):
        super().setUp()
        srv = server.Server(("127.0.0.1", 0), server.Handler)
        srv.store, srv.stop, srv.origins = self.store, threading.Event(), set()
        srv.sse_clients, srv.sse_lock = 0, threading.Lock()
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_port}"
        self.token = self.store.pair("relay unit")
        env = patch.dict(os.environ, {"Z0LIVE_LANES_FANOUT": "off"})
        env.start()
        self.addCleanup(env.stop)

    def test_launch_partial_final_gated_answer_and_hermes_dedupe(self):
        self.store.lanes.models = FakeModels({
            "qwen3-0.6b-q8": (0, '{"route":"answer","intent":"sum","kind":"math","complete":true}'),
            "qwen3-1.7b": (0, '{"route":"answer","intent":"sum","kind":"math","complete":true}'),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"It is 12.","kind":"math","confidence":0.9}'),
            "claude-opus-5-5": (0.3, '{"route":"answer","answer":"6 plus 7 is 13."}'),
        })

        async def run():
            relay = LaneRelay(self.token, base=self.base, choice="start live with 2 fast, 1 large, no council")
            started = await relay.start("clean")
            self.assertEqual(started["lanes"]["counts"], {"fast": 2, "small": 1, "large": 1, "council": 0})
            await relay.transcript("what is 6 plus", False)
            receipt = await relay.transcript("what is 6 plus 7", True)
            self.assertEqual((receipt["kind"], receipt["gated"], receipt["checking"]), ("math", True, True))
            said = await relay.answer("what is 6 plus 7")  # held until Opus has checked it
            self.assertEqual(said, "6 plus 7 is 13.")
            self.assertTrue(covered(said, "Six plus seven equals 13, so the answer is 13.", "what is 6 plus 7"))
            self.assertFalse(covered("It is 12.", "It is 13.", "what is 6 plus 7"))
            configured = await relay.transcript("switch to preset B", True)
            self.assertEqual(configured["configured"]["revision"], "preset-B-2f-2s-2l-1c")
            self.assertTrue((await relay.answer("switch to preset B")).startswith("Lanes set: 2 fast, 2 small"))
            await relay.close()
            return relay.counts

        counts = asyncio.run(run())
        self.assertEqual((counts["partials"], counts["finals"], counts["lane_answers_gated"]), (1, 2, 1))

    def test_no_token_file_means_no_relay(self):
        with patch("z0live_desktop.mobile_relay.TOKEN_FILE", Path(self.tmp.name) / "missing"):
            self.assertIsNone(LaneRelay.from_env())


if __name__ == "__main__":
    unittest.main()
