"""Recall on the call path (t_47576421): the records' cited block reaches the medium and slow lanes and the Hermes
turn, never the fast lane, and a missing, slow or broken lookup changes nothing."""
import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import call_recall
import mobile_live
import tiers

LIB = '''
def recall(question, before=None, k=6, refresh=False):
    if "boom" in question:
        raise RuntimeError("index gone")
    return {"hits": [{"locator": "pr:kvnloo/z0live#189", "ts": "2026-11-27", "title": "Live records"}]}


def render(res, max_chars=None):
    return "[header]\\n1. pr:kvnloo/z0live#189 (2026-11-27) Live records — status answers read the records"
'''


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="call-recall-"))
        (self.tmp / "recall.py").write_text(LIB)
        self.patches = [mock.patch.object(call_recall, "LIB", self.tmp / "recall.py"),
                        mock.patch.object(call_recall, "OFF", self.tmp / "seam-off"),
                        mock.patch.object(call_recall, "_mod", None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_block_carries_the_cited_lines_under_the_call_header(self):
        out = call_recall.lookup("did the live records pull request land yet")
        self.assertEqual(out["hits"], ["pr:kvnloo/z0live#189"])
        self.assertTrue(out["text"].startswith(call_recall.HEAD))
        self.assertIn("1. pr:kvnloo/z0live#189 (2026-11-27)", out["text"])
        self.assertNotIn("[header]", out["text"])

    def test_short_requests_and_the_off_switch_look_nothing_up(self):
        self.assertEqual(call_recall.lookup("name")["err"], "short")
        (self.tmp / "seam-off").write_text("")
        out = call_recall.lookup("did the live records pull request land yet")
        self.assertEqual((out["text"], out["err"]), ("", "off"))

    def test_a_missing_library_or_a_broken_index_is_no_block(self):
        self.assertIn("index gone", call_recall.lookup("boom boom boom boom")["err"])
        with mock.patch.object(call_recall, "LIB", self.tmp / "absent.py"), mock.patch.object(call_recall, "_mod", None):
            self.assertEqual(call_recall.lookup("did the live records pull request land yet")["err"], "no library")


class CallPathTests(unittest.IsolatedAsyncioTestCase):
    def call(self, **over):
        call = mobile_live.Call.__new__(mobile_live.Call)
        call.manager = types.SimpleNamespace(http=None)
        call.protocol = types.SimpleNamespace(exchanges=[])
        call.context_text, call.closed, call.latest_did, call.profile = "", False, "d1", "tek"
        call.counts, call.tasks, call.traces, call.call_recall = mobile_live.Counter(), set(), [], True
        call.trace = lambda event, **kw: call.traces.append((event, kw))
        call.say_first = lambda did, text: True
        call.ledger_brief = lambda did, *a, **kw: ""
        call.tier_claims_work = lambda did, text: False
        for key, value in over.items():
            setattr(call, key, value)
        return call

    async def lanes(self, call, lookup):
        seen = {}

        async def race(http, tier, bindings, context, request):
            seen[tier] = context
            return "", None, None, {}
        cfg = {"fast": [{"model": "f"}], "medium": [{"model": "m"}], "slow": [{"model": "s"}]}
        with mock.patch.object(tiers, "race", race), mock.patch.object(tiers, "load", lambda: cfg), \
                mock.patch.object(mobile_live, "live_picture", lambda: ""), \
                mock.patch.object(call_recall, "lookup", lookup):
            await call.tier_lanes("d1", "did the live records pull request land yet")
        return seen

    async def test_medium_and_slow_lanes_get_the_block_and_the_fast_lane_does_not(self):
        call = self.call()
        seen = await self.lanes(call, lambda request: {"text": "BLOCK", "hits": ["pr:x#1"], "s": 0.01, "err": ""})
        self.assertNotIn("BLOCK", seen["fast"])
        self.assertIn("BLOCK", seen["medium"])
        self.assertIn("BLOCK", seen["slow"])
        self.assertEqual([kw["hits"] for event, kw in call.traces if event == "recall.block"], [["pr:x#1"]])

    async def test_a_lookup_slower_than_the_wait_delays_no_lane(self):
        def slow(request):
            import time
            time.sleep(0.4)
            return {"text": "BLOCK", "hits": [], "s": 0.4, "err": ""}
        with mock.patch.object(call_recall, "LANE_WAIT", 0.05):
            seen = await self.lanes(self.call(), slow)
        self.assertEqual([("BLOCK" in seen[t]) for t in ("fast", "medium", "slow")], [False, False, False])

    async def test_recall_off_or_a_guest_call_looks_nothing_up(self):
        for over in ({"call_recall": False}, {"profile": "guest"}):
            def never(request):
                raise AssertionError("looked up")
            seen = await self.lanes(self.call(**over), never)
            self.assertEqual(set(seen), {"fast", "medium", "slow"})

    async def test_the_hermes_turn_gets_the_block_next_to_the_request(self):
        call = self.call(host_context=lambda did=None, ledger=True: "CALL CONTEXT")
        text = call.side_request("did it land", did="d1", recall="BLOCK")
        self.assertTrue(text.endswith("CALL CONTEXT\n\nBLOCK\n\nRequest: did it land"))
        self.assertNotIn("BLOCK", call.side_request("did it land", did="d1"))


if __name__ == "__main__":
    unittest.main()
