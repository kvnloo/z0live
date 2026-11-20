"""Guest voice: who holds the floor, when Grok is asked to speak, and what the phone hears. No network."""
import asyncio
import os
import unittest
from fractions import Fraction

import av
import numpy as np

import guest_voice


class FakeCall:
    closed = False

    def __init__(self):
        self.traces, self.sent = [], []

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
        self.raws = []

        async def raw(ev):
            self.raws.append(ev["type"])
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
        self.guest.on_event({"type": "response.done"})
        self.guest.until = 0  # Grok's hold is over, but the primary's muted reply is still playing
        self.assertTrue(self.guest.floor())
        self.assertEqual(self.call.sent, [])  # the note waits: the primary would read it out
        self.guest.observe({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Saturn."}}, "")
        self.assertFalse(self.guest.floor())
        self.assertIn("Saturn.", self.call.sent[0]["content"][0]["text"])

    async def test_wanted_reads_env_and_file(self):
        os.environ["Z0LIVE_GUEST"] = "off"
        self.assertEqual(guest_voice.wanted(), "")
        os.environ["Z0LIVE_GUEST"] = "grok"
        self.assertEqual(guest_voice.wanted(), "grok")
        del os.environ["Z0LIVE_GUEST"]


if __name__ == "__main__":
    unittest.main()
