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
                    "arguments": json.dumps({"request": "The user's calendar today"})}]:
            leg.on_event(ev)
        self.assertEqual([e["type"] for e in leg.call.got],
                         ["session.started", "input_transcript.added", "turn.done", "output_transcript.added",
                          "turn.done", "delegation.created"])
        self.assertEqual(leg.call.got[2]["turn"], {"role": "user", "transcript": "what's on my calendar"})
        self.assertEqual(leg.call.got[-1]["item"], {"id": "c1", "target": "client",
                                                    "content": [{"type": "text", "text": "The user's calendar today"}]})

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
                      "content": [{"type": "input_text", "text": "Call brief: The user approved adb."}]})
        await asyncio.sleep(0.01)
        self.assertEqual(leg.sent, [{"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": "Call brief: The user approved adb."}]}}])

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


if __name__ == "__main__":
    unittest.main()
