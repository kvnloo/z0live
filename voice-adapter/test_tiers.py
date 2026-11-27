"""Tier lanes: within a tier the first real answer wins; across tiers the first answer is spoken, a later one only
when it adds something, and nothing is spoken once Hermes answered or a newer request started. No network."""
import asyncio
import os
import unittest
from collections import Counter, deque
from unittest import mock

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402
import tiers  # noqa: E402

FAST = [{"provider": "p", "model": "haiku"}, {"provider": "p", "model": "luna"}]


def fake_ask(answers, delays=None):
    async def ask(http, binding, system, user, timeout):
        await asyncio.sleep((delays or {}).get(binding["model"], 0))
        out = answers[binding["model"]]
        if isinstance(out, Exception):
            raise out
        return {"text": out, "first_s": 0.1, "total_s": 0.1}
    return ask


class RaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_and_errors_lose_to_a_real_answer(self):
        ask = fake_ask({"haiku": "SKIP", "luna": "About 70 degrees."}, {"luna": 0.05})
        with mock.patch.object(tiers, "ask", ask):
            text, model, _, errors = await tiers.race(None, "fast", FAST, "", "q")
        self.assertEqual((text, model), ("About 70 degrees.", "luna"))
        self.assertEqual(errors, {"haiku": "SKIP"})

    async def test_skip_anywhere_is_a_skip_but_a_lowercase_skip_is_speech(self):
        ask = fake_ask({"haiku": "I can't send that message for you right now. SKIP", "luna": "You can skip breakfast."})
        with mock.patch.object(tiers, "ask", ask):
            text, model, _, _ = await tiers.race(None, "fast", FAST, "", "q")
        self.assertEqual((text, model), ("You can skip breakfast.", "luna"))

    async def test_no_answer_when_every_lane_skips_or_fails(self):
        ask = fake_ask({"haiku": "SKIP", "luna": RuntimeError("no Codex credentials")})
        with mock.patch.object(tiers, "ask", ask):
            text, model, _, errors = await tiers.race(None, "fast", FAST, "", "q")
        self.assertEqual((text, model), ("", None))
        self.assertEqual(set(errors), {"haiku", "luna"})

    def test_every_lane_sees_the_brief_and_the_call_so_far(self):
        ctx = tiers.snapshot("Kevin approved adb.", [("user", "hey"), ("assistant", "Hi Kevin.")])
        self.assertIn("Kevin approved adb.", ctx)
        self.assertIn("Kevin: hey\nYou: Hi Kevin.", ctx)


class Call:
    """Just what Call.tier_lanes touches."""
    tier_lanes = live.Call.tier_lanes
    say_first = live.Call.say_first
    ledger_brief = live.Call.ledger_brief
    ledger_item = live.Call.ledger_item
    tier_claims_work = live.Call.tier_claims_work
    ledger_call = live.Call.ledger_call
    claims_work = live.Call.claims_work

    def __init__(self):
        self.id, self.item_of = "call-x", {}
        self.closed, self.context_text, self.counts, self.spoke, self.events = False, "", Counter(), [], []
        self.protocol = type("P", (), {"exchanges": deque()})()
        self.manager = type("M", (), {"http": None})()
        self.latest_did = "d1"

    def speak_for(self, did, text, lane=False):
        self.spoke.append(text)

    def trace(self, ev, **kw):
        self.events.append(ev)


CFG = {"fast": [{"provider": "p", "model": "haiku"}], "medium": [{"provider": "p", "model": "sonnet"}],
       "slow": [{"provider": "p", "model": "opus"}]}


class SpeakTests(unittest.IsolatedAsyncioTestCase):
    async def run_lanes(self, call, answers, delays):
        with mock.patch.object(tiers, "ask", fake_ask(answers, delays)), mock.patch.object(tiers, "load", lambda: CFG):
            await call.tier_lanes("d1", "boiling point on Everest?")

    async def test_only_the_first_tier_answer_is_spoken(self):
        # Later tiers phrase the same answer differently; speaking them made the voice merge versions (11-20).
        call = Call()
        await self.run_lanes(call, {"haiku": "Water boils at about 70 C on Everest.",
                                    "sonnet": "On Everest water boils at about 70 C.",
                                    "opus": "About 70 C; pressure there is a third of sea level."},
                             {"haiku": 0, "sonnet": 0.02, "opus": 0.04})
        self.assertEqual(call.spoke, ["Water boils at about 70 C on Everest."])

    async def test_quiet_once_hermes_answered(self):
        call = Call()
        call.hermes_done = {"d1"}
        await self.run_lanes(call, {"haiku": "a", "sonnet": "b", "opus": "c"}, {})
        self.assertEqual(call.spoke, [])

    async def test_quiet_once_a_newer_request_started(self):
        call = Call()
        call.latest_did = "d2"
        await self.run_lanes(call, {"haiku": "a", "sonnet": "b", "opus": "c"}, {})
        self.assertEqual(call.spoke, [])


if __name__ == "__main__":
    unittest.main()
