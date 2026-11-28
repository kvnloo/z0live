"""Replay final transcripts through the call adapter, with offline I/O boundaries."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

import lane_layout as layout
import mobile_live as live


class SpokenActions(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp) / "layout.json"
        self.enterContext(mock.patch.object(layout, "FILE", self.path))
        self.enterContext(mock.patch.object(layout, "TIERS_FILE", Path(tmp) / "no-tiers"))
        self.enterContext(mock.patch.object(layout, "POOL_FILE", Path(tmp) / "no-pool"))
        self.enterContext(mock.patch.dict(os.environ, {"Z0LIVE_LEDGER": str(Path(tmp) / "ledger")}))
        self.call = live.Call(live.Manager(None), profile="tek")
        self.addAsyncCleanup(self.call.close, "test")
        self.events, self.speech = [], []
        trace = self.call.trace
        self.call.trace = mock.Mock(wraps=trace, side_effect=lambda name, **kw: self.events.append(name))
        self.call.trace.delegations = trace.delegations
        self.call.speak_for = mock.Mock(side_effect=lambda did, text, lane=False: self.speech.append(text))
        self.call.send = mock.Mock()
        self.call.ask_hermes = mock.AsyncMock(return_value="No action.")
        self.call.tier_lanes = mock.AsyncMock()
        self.call.fable_lanes = mock.AsyncMock()
        self.call.run_fable_lanes = mock.AsyncMock()
        self.call.ttft_lane = mock.AsyncMock()

    def final(self, text, segment=None):
        self.call.message(json.dumps({"type": "turn.created", "turn": {"role": "user"}}), True)
        if segment:
            self.call.message(json.dumps({"type": "delegation.created", "item": {
                "target": "client", "id": "sidecar", "content": [{"text": segment}]}}), True)
        self.call.message(json.dumps({"type": "turn.done", "turn": {
            "role": "user", "id": "replayed", "transcript": text}}), True)

    async def test_nov28_attach_five_fast_lanes(self):
        # 11-28 10:15 call, turn 4. The provider sidecar is a paraphrase, not another action.
        self.call.protocol.host_delegation = True
        self.final("Can you attach five fast lanes to this conversation", "Configure the voice conversation with fast lanes")
        await asyncio.sleep(0)
        self.assertIn("lanes.layout", self.events)
        self.assertNotIn("delegation.start", self.events)
        self.assertEqual(layout.current()[0]["voice"]["fast"], {"terra": 5})
        self.assertEqual(len(self.speech), 1)
        self.assertIn("five fast lanes, all Terra", self.speech[0])

    async def test_polite_model_command_with_late_sidecar(self):
        self.call.protocol.host_delegation = True
        self.final("Okay, could you please attach three Haiku lanes to this call, thanks?")
        self.call.message(json.dumps({"type": "delegation.created", "item": {
            "target": "client", "id": "late", "content": [{"text": "Set up helpers for the call"}]}}), True)
        await asyncio.sleep(0)
        self.assertEqual(layout.current()[0]["voice"]["fast"], {"terra": 2, "haiku": 3})
        self.assertEqual(len(self.speech), 1)
        self.assertIn("three Haiku", self.speech[0])
        self.assertNotIn("delegation.start", self.events)

    async def test_bot_requests_without_provider_delegation(self):
        relay = self.enterContext(mock.patch.object(live.ask_bot, "ask", return_value={"ok": True, "say": "Bot answer."}))
        self.enterContext(mock.patch.object(live.ask_bot, "hosts", return_value={"pc": 1, "cfg": 2}))
        for text, profile in (("@pc, check the drive", "pc"), ("Ask the PC bot to list the drive", "pc"),
                              ("tell cfg to check the config", "cfg"),
                              ("Can you see if the PC bot is able to access the drive", "pc")):
            with self.subTest(text=text):
                self.final(text)
                for _ in range(100):
                    if self.speech and self.speech[-1] == "Bot answer.":
                        break
                    await asyncio.sleep(.01)
                self.assertIsNotNone(relay.call_args)
                self.assertEqual(relay.call_args.args[0], profile)
                self.assertIn("Bot answer.", self.speech)
                self.speech.clear()
        self.assertEqual(relay.call_count, 4)

    async def test_failed_relay_say_is_not_spoken(self):
        self.enterContext(mock.patch.object(live.ask_bot, "hosts", return_value={"pc": 1}))
        self.enterContext(mock.patch.object(live.ask_bot, "ask", return_value={"ok": False, "say": "False success."}))
        await self.call.host_delegate("d1", "@pc check the drive")
        for _ in range(100):
            if "ask.done" in self.events:
                break
            await asyncio.sleep(.01)
        self.assertIn("ask.done", self.events)
        self.assertNotIn("False success.", self.speech)
        self.assertFalse(any(s.startswith("Sent to") for s in self.speech))

    async def test_conductor_names_only_started_lanes_once(self):
        state = self.path.with_name("state.json")
        self.enterContext(mock.patch.object(live.tiers, "CONDUCTOR_STATE", state))
        self.enterContext(mock.patch.object(live.lane_cards, "POLL_SECONDS", .01))
        self.call.card_floor_free = mock.Mock(return_value=True)
        request = "Research that layout"
        key = live.lane_cards.call_key(self.call.id)
        lanes = [{"id": str(i), "model": "haiku 5.5", "state": "running"} for i in range(5)]
        lanes += [{"id": "q", "model": "opus", "state": "queued"},
                  {"id": "f", "model": "opus", "state": "failed"}]
        data = {"asks": [{"id": "a1", "call": key, "text": request, "lanes": lanes},
                         {"id": "other", "call": "other", "text": request, "lanes": lanes}]}
        state.write_text(json.dumps(data))
        self.call.watch_lane_cards("d1", request)
        for _ in range(50):
            if self.speech:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.speech, ["Five Haiku lanes started."])
        state.write_text(json.dumps(data) + "\n")
        await asyncio.sleep(.05)
        self.assertEqual(self.speech, ["Five Haiku lanes started."])
