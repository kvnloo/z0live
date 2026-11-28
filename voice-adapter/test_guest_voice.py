"""Guest voice: who holds the floor, when Grok is asked to speak, and what the phone hears. No network."""
import asyncio
from collections import deque
import json
import os
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import av
import numpy as np

import guest_voice
import mobile_live


class FakeCall:
    closed = False

    def __init__(self):
        self.traces, self.sent = [], []
        self.protocol = SimpleNamespace(exchanges=deque(maxlen=8))

    def trace(self, ev, **kw):
        self.traces.append(ev)

    def send(self, event, to_provider=False):
        self.sent.append(event)


def frame(layout="stereo", value=7, pts=960):
    f = av.AudioFrame(format="s16", layout=layout, samples=960)
    f.planes[0].update(np.full(960 * len(f.layout.channels), value, dtype=np.int16).tobytes())
    f.sample_rate, f.pts, f.time_base = 48000, pts, Fraction(1, 48000)
    return f


class Source:
    def __init__(self, f):
        self.f = f

    async def recv(self):
        return self.f


class GuestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.call = FakeCall()
        self.guest = guest_voice.GuestVoice(self.call)
        self.guest.ready = True
        self.raws = []

        async def raw(event, **_):
            self.raws.append(event["type"])
        self.guest.raw = raw

    async def settle(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_name_claims_floor_but_asks_only_at_turn_end(self):
        self.guest.observe({"type": "input_transcript.added"}, "Grok, what's your")
        await self.settle()
        self.assertTrue(self.guest.floor())
        self.assertEqual(self.raws, [])  # do not answer mid-sentence
        self.guest.observe({"type": "turn.done", "turn": {"role": "user"}}, "")
        await self.settle()
        self.assertEqual(self.raws, ["response.create"])
        self.guest.on_event({"type": "input_audio_buffer.committed"})  # Grok's own VAD: no second reply
        await self.settle()
        self.assertEqual(self.raws, ["response.create"])

    async def test_name_from_an_earlier_turn_does_not_claim(self):
        running = "Grok, what's your favorite planet"
        self.guest.observe({"type": "input_transcript.added"}, running)
        self.guest.on_event({"type": "input_audio_buffer.committed"})  # GPT-Live stays silent: no user turn.done
        self.guest.on_event({"type": "response.done"})
        await self.settle()
        self.guest.until = 0  # Grok's reply finished and the hold expired
        self.guest.observe({"type": "input_transcript.added"}, running + " And Hermes, what do you think")
        self.assertFalse(self.guest.addressed)
        self.assertFalse(self.guest.floor())

    async def test_unaddressed_reply_is_cancelled_and_primary_keeps_floor(self):
        self.guest.observe({"type": "input_transcript.added"}, "what's the capital of France")
        self.guest.on_event({"type": "response.created"})
        await self.settle()
        self.assertEqual(self.raws, ["response.cancel"])
        self.assertFalse(self.guest.floor())
        primary = frame(value=7)
        out = await guest_voice.FloorSwitch(Source(primary), self.guest).recv()
        self.assertIs(out, primary)

    async def test_phone_hears_grok_in_primary_layout_then_silence(self):
        self.guest.claim()
        self.guest.output.buf.extend(np.full(960, 1000, dtype=np.int16).tobytes())
        switch = guest_voice.FloorSwitch(Source(frame("stereo", value=7, pts=4800)), self.guest)
        out = await switch.recv()
        self.assertEqual((out.layout.name, out.samples, out.pts), ("stereo", 960, 4800))
        self.assertTrue((np.frombuffer(bytes(out.planes[0]), np.int16)[: 960 * 2] == 1000).all())
        quiet = await switch.recv()  # Grok's buffer is empty but it still holds the floor: not the primary
        self.assertFalse(np.frombuffer(bytes(quiet.planes[0]), np.int16)[: 960 * 2].any())

    async def test_primary_reply_on_grok_turn_is_muted_whole_and_note_waits(self):
        self.guest.claim()
        self.guest.on_event({"type": "input_audio_buffer.committed"})
        self.guest.observe({"type": "output_transcript.added"}, "")  # GPT-Live answers anyway
        self.guest.addressed = True
        self.guest.on_event({"type": "response.output_audio_transcript.done", "transcript": "Saturn."})
        self.guest.output.buf.extend(b"\x01\x00" * 960)
        await guest_voice.FloorSwitch(Source(frame()), self.guest).recv()
        self.guest.on_event({"type": "response.done"})
        await self.settle()
        self.guest.until = 0  # Grok's hold is over, but the primary's muted reply is still playing
        self.assertTrue(self.guest.floor())
        self.assertEqual(self.call.sent, [])  # the note waits: the primary would read it out
        self.guest.observe({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Saturn."}}, "")
        self.assertFalse(self.guest.floor())
        self.assertIn("Saturn.", self.call.sent[0]["content"][0]["text"])
        self.assertEqual(list(self.call.protocol.exchanges), [("Grok", "Saturn.")])

    async def test_wanted_reads_env_and_file(self):
        os.environ["Z0LIVE_GUEST"] = "off"
        self.assertEqual(guest_voice.wanted(), "")
        os.environ["Z0LIVE_GUEST"] = "grok"
        self.assertEqual(guest_voice.wanted(), "grok")
        del os.environ["Z0LIVE_GUEST"]


class SharedContextTest(unittest.IsolatedAsyncioTestCase):
    """Grok gets the brief and running summary the primary has (11-28: one running thread for both voices)."""

    async def asyncSetUp(self):
        self.call = FakeCall()
        self.context = "Brief: Kevin's codeword today is PELICAN."
        self.call.guest_context = lambda: self.context
        self.guest = guest_voice.GuestVoice(self.call)
        self.guest.ready = True
        self.call.refresh_guest_context = lambda: self.guest.share(self.context, "turns")
        self.raws = []

        async def raw(event, **_):
            self.raws.append(event)
        self.guest.raw = raw

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)

    def updates(self):
        return [e["session"]["instructions"] for e in self.raws if e["type"] == "session.update"]

    async def test_start_refresh_puts_the_brief_in_grok_instructions_once(self):
        self.call.manager = SimpleNamespace(http=SimpleNamespace(ws_connect=AsyncMock()))
        self.guest.reader = AsyncMock()
        with patch.object(guest_voice.grok_leg, "mint_secret", return_value="fixture"):
            await self.guest.start(self.context)
        self.guest.share(self.context, "turns")
        await self.settle()
        [text] = self.updates()
        self.assertIn("PELICAN", text)
        self.assertTrue(text.startswith(self.guest.persona))
        self.assertIn("guest.context", self.call.traces)

    async def test_refresh_replaces_the_context_not_appends(self):
        self.guest.share(self.context, "start")
        self.context = "Brief: the codeword is now HERON."
        self.guest.share(self.context, "renewal")
        await self.settle()
        last = self.updates()[-1]
        self.assertIn("HERON", last)
        self.assertNotIn("PELICAN", last)

    async def test_refresh_waits_while_grok_answers_then_applies(self):
        self.guest.claim()
        self.guest.ask()
        self.guest.share(self.context, "renewal")
        self.guest.share("Newest snapshot only", "turns")
        await self.settle()
        self.assertEqual(self.updates(), [])  # a session change mid-reply could cut Grok off
        self.guest.on_event({"type": "response.done", "response": {"status": "completed"}})
        await self.settle()
        [text] = self.updates()
        self.assertIn("Newest snapshot only", text)
        self.assertNotIn("PELICAN", text)

    async def test_summary_follows_the_call_every_few_exchanges(self):
        for i in range(guest_voice.SHARE_EVERY):
            self.context = f"Call so far: turn {i}"
            self.guest.observe({"type": "turn.done", "turn": {"role": "assistant", "transcript": f"ok {i}"}}, "")
        await self.settle()
        self.assertIn(f"turn {guest_voice.SHARE_EVERY - 1}", self.updates()[-1])

    async def test_refresh_routes_same_snapshot_to_both_voices(self):
        self.call.guest = self.guest
        self.call.brief = "Opening brief"
        self.call.call_recap = mobile_live.Call.call_recap
        self.call.protocol.exchanges.append(("user", "Summary-only fact: lavender compass"))
        self.call.ledger_brief = lambda **_: "Current request ledger"
        primary = []
        self.call.guest_note = primary.append
        self.call.guest_context = lambda: mobile_live.Call.guest_context(self.call)
        with patch.object(mobile_live, "live_picture", return_value="TV is idle") as live:
            mobile_live.Call.refresh_guest_context(self.call)
        live.assert_called_once()
        await self.settle()
        [snapshot] = primary
        self.assertIn(f"<shared_call_context>\n{snapshot}\n</shared_call_context>", self.updates()[-1])
        for fact in ("Opening brief", "lavender compass", "TV is idle", "Current request ledger"):
            self.assertIn(fact, snapshot)

    async def test_hermes_turn_note_has_no_speakable_label(self):
        # 13:39 call: Grok said "Same line now. Hermes said: Cool..." because the note began "Hermes said:"
        self.guest.observe({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Cool."}}, "")
        await self.settle()
        note = [e for e in self.raws if e["type"] == "conversation.item.create"][0]["item"]["content"][0]["text"]
        self.assertFalse(note.startswith("Hermes said"))
        self.assertIn("never read aloud", note)
        self.assertIn("Never say a note's label", self.guest.persona)

    async def test_primary_is_not_told_empty_grok_reply_played(self):
        # 13:39 call: GPT-Live said "Grok's audio might be on a different route"
        self.assertIn("same audio line", guest_voice.PRIMARY_NOTE)
        self.guest.claim()
        self.guest.ask()
        self.guest.on_event({"type": "response.output_audio_transcript.done", "transcript": "Saturn."})
        self.guest.on_event({"type": "response.done", "response": {"status": "cancelled",
                                                                   "status_details": {"reason": "turn_detected"}}})
        await self.settle()
        self.assertIn("guest.done", self.call.traces)
        text = self.call.sent[0]["content"][0]["text"]
        self.assertIn("did not play audible audio", text)
        self.assertNotIn("Saturn.", text)
        self.assertFalse(self.call.protocol.exchanges)


class RoleConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = Path(self.dir.name) / "voices.json"
        os.environ["Z0LIVE_VOICES_FILE"] = str(self.file)
        os.environ.pop("Z0LIVE_GUEST", None)
        os.environ.pop("Z0LIVE_GUEST_ROLE", None)

    def tearDown(self):
        os.environ.pop("Z0LIVE_VOICES_FILE", None)
        self.dir.cleanup()

    def test_legacy_string_is_the_plain_guest(self):
        self.file.write_text(json.dumps({"guest": "grok"}))
        config = guest_voice.configured()
        self.assertEqual((config.engine, config.role), ("grok", "guest"))
        self.assertTrue(config.persona().startswith(guest_voice.PERSONA))

    def test_intent_manager_role_and_template(self):
        template = Path(self.dir.name) / "intent.md"
        template.write_text("North star: ship z0live.")
        self.file.write_text(json.dumps({"guest": {"engine": "grok", "role": "intent-manager",
                                                   "template": str(template)}}))
        config = guest_voice.configured()
        self.assertEqual(config.role, "intent-manager")
        self.assertIn("intent manager", config.persona())
        self.assertIn("North star: ship z0live.", config.persona())
        self.assertIn("chief of staff", config.grok_primary_note)
        self.assertEqual(guest_voice.GuestVoice(FakeCall(), config).persona, config.persona())

    def test_unknown_role_falls_back_and_missing_template_is_ignored(self):
        self.file.write_text(json.dumps({"guest": {"engine": "grok", "role": "boss", "template": "/nope/x.md"}}))
        config = guest_voice.configured()
        self.assertEqual(config.role, "guest")
        self.assertNotIn("intent_template", config.persona())


if __name__ == "__main__":
    unittest.main()