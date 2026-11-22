"""Grok Voice leg: Grok's Realtime events become the call's subscription dialect, and Hermes answers go back
as one tool result per request (later answers as spoken updates). No network."""
import asyncio
import json
import unittest
from types import SimpleNamespace

import grok_leg


class FakeCall:
    def __init__(self):
        self.got, self.muted, self.closed, self.traced = [], False, False, []

    def message(self, raw, from_provider):
        self.got.append(json.loads(raw))

    def trace(self, ev, **data):
        self.traced.append((ev, data))


class Leg(grok_leg.GrokLeg):
    def __init__(self):
        super().__init__(FakeCall(), "persona")
        self.sent = []

    async def raw(self, event, **_):
        self.sent.append(event)


class GrokLegTests(unittest.IsolatedAsyncioTestCase):
    def test_grok_events_arrive_in_the_calls_dialect(self):
        leg = Leg()
        for ev in [{"type": "session.updated"},
                   {"type": "conversation.item.input_audio_transcription.completed", "transcript": "what's on my calendar"},
                   {"type": "response.output_audio_transcript.delta", "delta": "Checking."},
                   {"type": "response.output_audio_transcript.done", "transcript": "Checking."},
                   {"type": "response.function_call_arguments.done", "name": "ask_hermes", "call_id": "c1",
                    "arguments": json.dumps({"request": "Kevin's calendar today"})}]:
            leg.on_event(ev)
        self.assertEqual([e["type"] for e in leg.call.got],
                         ["session.started", "input_transcript.added", "turn.done", "output_transcript.added",
                          "turn.done", "delegation.created"])
        self.assertEqual(leg.call.got[2]["turn"], {"role": "user", "transcript": "what's on my calendar"})
        self.assertEqual(leg.call.got[-1]["item"], {"id": "c1", "target": "client",
                                                    "content": [{"type": "text", "text": "Kevin's calendar today"}]})

    async def test_first_answer_is_the_tool_result_and_a_second_is_an_update(self):
        leg = Leg()
        for chunk in ('"Agent Final Message":\n\nDentist ', "at 3pm."):  # one answer split by chunks()
            leg.outbound({"type": "delegation.context.append", "delegation_item_id": "c1", "channel": "speakable",
                          "content": [{"type": "input_text", "text": chunk}]})
        await asyncio.sleep(0.01)
        leg.outbound({"type": "delegation.context.append", "delegation_item_id": "c1", "channel": "speakable",
                      "content": [{"type": "input_text", "text": "Also gym at 6."}]})
        await asyncio.sleep(0.01)
        kinds = [(e["type"], e.get("item", {}).get("type")) for e in leg.sent]
        self.assertEqual(kinds, [("conversation.item.create", "function_call_output"), ("response.create", None),
                                 ("conversation.item.create", "message"), ("response.create", None)])
        self.assertEqual(leg.sent[0]["item"], {"type": "function_call_output", "call_id": "c1", "output": "Dentist at 3pm."})
        self.assertIn("Also gym at 6.", leg.sent[2]["item"]["content"][0]["text"])

    async def test_background_context_is_a_silent_system_item(self):
        leg = Leg()
        leg.outbound({"type": "session.context.append", "channel": "developer",
                      "content": [{"type": "input_text", "text": "Call brief: Kevin approved adb."}]})
        await asyncio.sleep(0.01)
        self.assertEqual(leg.sent, [{"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "Call brief: Kevin approved adb."}]}}])

    def test_barge_in_drops_queued_speech(self):
        leg = Leg()
        leg.output.feed(bytes(4800))
        self.assertTrue(leg.output.buf)
        leg.on_event({"type": "input_audio_buffer.speech_started"})
        self.assertFalse(leg.output.buf)

    def test_turn_timing_is_traced_once_per_response(self):
        leg = Leg()
        for ev in [{"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 4200},
                   {"type": "response.created", "response": {"id": "r1"}},
                   {"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAAA" * 160},
                   {"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAAA" * 160}]:
            leg.on_event(ev)
        self.assertEqual([(d["type"], d["response_id"], d["audio_end_ms"]) for _, d in leg.call.traced],
                         [("input_audio_buffer.speech_stopped", None, 4200), ("response.created", "r1", None),
                          ("response.output_audio.delta", "r1", None)])


class Mic:
    """A phone track: N 20 ms 48 kHz stereo frames of a deterministic ramp, then the track ends."""

    def __init__(self, n):
        import numpy as np
        self.frames, self.n, self.np = [], n, np

    async def recv(self):
        import av
        from fractions import Fraction
        np = self.np
        i = len(self.frames)
        if i >= self.n:
            raise EOFError
        pcm = ((np.arange(960 * 2, dtype=np.int64) + i * 7919) % 20000 - 10000).astype(np.int16).reshape(1, -1)
        f = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
        f.sample_rate, f.pts, f.time_base = 48000, i * 960, Fraction(1, 48000)
        self.frames.append(f)
        return f


def resampled(frames):
    import av
    r = av.AudioResampler(format="s16", layout="mono", rate=grok_leg.RATE_IN)
    return b"".join(bytes(o.planes[0])[: o.samples * 2] for f in frames for o in r.resample(f))


class UplinkAppendTests(unittest.IsolatedAsyncioTestCase):
    async def pump(self, per_packet, n=25, backlog=0):
        import base64
        from unittest.mock import patch
        leg = Leg()
        leg.ready = True
        mic = Mic(n)
        with patch.object(grok_leg, "APPEND_PER_PACKET", per_packet), \
                patch.object(grok_leg.GrokLeg, "send_backlog", lambda self: backlog):
            await leg.pump(mic)
        sent = [base64.b64decode(e["audio"]) for e in leg.sent if e["type"] == "input_audio_buffer.append"]
        return sent, resampled(mic.frames)

    async def test_per_packet_flushes_each_frame_and_keeps_every_byte(self):
        sent, want = await self.pump(True)
        self.assertGreaterEqual(len(sent), 24)  # one append per received frame (resampler may hold the first)
        self.assertTrue(all(len(s) <= 2 * 960 for s in sent))
        self.assertEqual(b"".join(sent), want)

    async def test_backed_up_socket_coalesces_to_100ms_without_loss(self):
        sent, want = await self.pump(True, backlog=4096)
        self.assertTrue(all(len(s) >= grok_leg.BATCH_BYTES for s in sent))
        self.assertEqual(b"".join(sent), want[: len(b"".join(sent))])
        self.assertLess(len(want) - len(b"".join(sent)), grok_leg.BATCH_BYTES)

    async def test_batching_off_sends_100ms_appends(self):
        sent, want = await self.pump(False)
        self.assertEqual(len(sent), 4)
        self.assertTrue(all(len(s) >= grok_leg.BATCH_BYTES for s in sent))
        self.assertEqual(b"".join(sent), want[: len(b"".join(sent))])


if __name__ == "__main__":
    unittest.main()
