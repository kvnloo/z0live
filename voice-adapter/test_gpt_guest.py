"""Two GPT-Live legs share the phone's audio and transcript floor."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import guest_voice
from guest_floor import Address, Addressing, Answer, Floor
import mobile_live as live
from test_guest_voice import FakeCall, frame


def turn(kind, text, start=0, end=600, *, role="user", tid="user"):
    return {"type": f"turn.{kind}", "turn": {"id": tid, "role": role, "transcript": text,
                                              "start_ms": start, "end_ms": end}}


def output(text):
    return {"type": "output_transcript.added", "item": {"text": text}}


class FloorTest(unittest.TestCase):
    def test_addressing(self):
        parse = Addressing(("tek", "teknium", "டெக்", "டெக்னியம்"))
        for text, expected in [
            ("Hey TEK, what do you think", Address.GUEST), ("teknium", Address.GUEST),
            ("One thing, Tek, what shipped?", Address.GUEST), ("டெக், என்ன?", Address.GUEST),
            ("டெக்னியம், என்ன?", Address.GUEST), ("tell tek later", None),
            ("Tek worked on it", None), ("Hey Hermes, a question", Address.OTHER),
            ("Back to Julia", Address.OTHER), ("Tek, wait. Hermes, you take this.", Address.OTHER),
        ]:
            with self.subTest(text=text):
                self.assertIs(parse(text, final=True), expected)
        self.assertIsNone(parse("Hey Tek", final=False))

    def test_exchange_transitions(self):
        for scenario, text, began, finished, interrupted, now, expected in [
            ("mid-sentence pause", "what shipped today", 1, 3, False, 4, "guest"),
            ("backchannel", "Mm-mm", 4, 3, False, 5, "guest"),
            ("guest working", "one more detail", 4, None, False, 5, "guest"),
            ("fresh unnamed turn", "what is twelve times twelve", 4, 3, False, 5, "primary"),
            ("barge-in", "wait, different question", 2, None, True, 3, "primary"),
        ]:
            with self.subTest(scenario=scenario):
                floor = Floor(0, lambda *a, **k: None, available=True)
                floor.user_turn(Address.GUEST, "Tek, what", 1, 1, final=False)
                if finished is not None:
                    floor.reply_finished("guest", finished, finished)
                floor.user_turn(None, text, began, now, final=True, interrupted=interrupted)
                self.assertEqual(floor.owner, expected)

    def test_silence_starts_after_the_reply(self):
        floor = Floor(0, lambda *a, **k: None, available=True)
        floor.user_turn(Address.GUEST, "Tek, what", 1, 1, final=True)
        floor.reply_finished("guest", 15, 12)
        floor.expire(34.99)
        self.assertEqual(floor.owner, "guest")
        floor.expire(35)
        self.assertEqual((floor.owner, floor.reason), ("primary", "guest_idle"))


class PrimaryCall(FakeCall):
    id = "test-call"
    provider_ready = heard_user = True
    input_loud_at = output_loud_at = float("-inf")

    def __init__(self):
        super().__init__()
        self.protocol = live.Protocol(host_delegation=True)
        self.tasks = []
        self.manager = type("Manager", (), {"http": None})()

    def spawn(self, coro):
        self.tasks.append(asyncio.create_task(coro))

    def guest_note(self, text):
        self.sent.append(text)


class GPTGuestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.call = PrimaryCall()
        self.guest = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
        self.addAsyncCleanup(self.guest.close)
        self.guest.message(json.dumps({"type": "session.started"}))
        self.guest.send = self.call.sent.append
        self.call.guest = self.guest

    async def test_suppressed_reply_never_leaks_audio_text_or_context(self):
        self.guest.observe("guest", turn("done", "Tek, what shipped?"))
        self.assertFalse(self.guest.observe("primary", output("Not for Kevin.")))
        self.guest.message(json.dumps(output("A release.")))
        self.guest.audio.extend(b"\xe8\x03" * 960)
        self.assertTrue((self.guest.output(frame()).to_ndarray() == 1000).all())
        self.assertFalse(self.guest.output(frame()).to_ndarray().any())
        self.guest.observe("primary", turn("done", "Hermes, take over", 2000, 2600, tid="redirect"))
        self.assertFalse(self.guest.observe("primary", output("Still the same hidden reply.")))
        self.assertFalse(self.guest.output(frame()).to_ndarray().any())
        self.guest.observe("primary", turn("done", "Not for Kevin.", role="assistant", tid="hidden"))
        self.assertTrue(self.guest.observe("primary", output("New reply.")))
        self.assertTrue((self.guest.output(frame()).to_ndarray() == 7).all())
        self.assertFalse(any("Not for Kevin" in str(msg) for msg in self.call.sent))

    async def test_cumulative_final_does_not_reclaim_old_address(self):
        g = self.guest
        g.observe("primary", turn("created", "Tek, what shipped?", 1000, 1800))
        g.observe("guest", output("A release."))
        g.observe("guest", turn("done", "A release.", role="assistant", tid="spoken"))
        g.observe("primary", {"type": "turn.delta", "turn_id": "user", "delta": " Thanks. What's twelve times twelve?",
                              "start_ms": 5000, "end_ms": 6200})
        g.observe("primary", turn("done", "Tek, what shipped? Thanks. What's twelve times twelve?", 1000, 6200))
        self.assertEqual((g.floor.owner, g.floor.reason), ("primary", "new_user_turn"))
        self.assertTrue(any("A release." in str(note) for note in self.call.sent))

    async def test_barge_in_on_either_leg_but_not_echo_or_backchannel(self):
        for source in ("primary", "guest"):
            with self.subTest(source=source):
                g = self.guest
                g.floor.change(source, "test", live.time.monotonic())
                self.assertTrue(g.observe(source, output("Here is the answer so far.")))
                self.call.output_loud_at = live.time.monotonic()
                for i, (text, length) in enumerate((("Here is the answer", 600), ("Mm-mm", 600), ("noise", 350))):
                    g.observe("primary", turn("created", text, 1000 * i, 1000 * i + length, tid=str(i)))
                    self.assertTrue(g.observe(source, output(" More words.")))
                g.observe("primary", turn("created", "Wait, different question", 3000, 3800, tid="barge"))
                self.assertFalse(g.observe(source, output(" Stop forwarding this.")))
                self.assertFalse(g.output(frame()).to_ndarray().any())
                g.observe(source, turn("done", "", role="assistant", tid="cut"))

    async def test_late_guest_answer_and_primary_answer_wait_for_their_floors(self):
        g = self.guest
        g.observe("guest", turn("done", "Tek, what shipped?", 1000, 2000))
        result = asyncio.get_running_loop().create_future()
        with patch.object(live, "ask_guest_host", AsyncMock(side_effect=lambda *args: result.result())) as host:
            g.message(json.dumps({"type": "delegation.created", "item": {
                "id": "guest-id", "target": "client", "content": [{"text": "What shipped?"}]}}))
            self.call.protocol.seen.append("primary-id")
            live.Call.speak_for(self.call, "primary-id", "Primary answer.")
            self.assertFalse(any("Primary answer" in str(msg) for msg in self.call.sent))
            g.observe("primary", turn("done", "Hermes, your turn", 3000, 4000, tid="redirect"))
            result.set_result("A release.")
            await asyncio.gather(*self.call.tasks)
            self.assertEqual(host.await_args.args[1:], ("tek", "z0live-guest-test-call", "What shipped?"))
        self.assertTrue(any("Primary answer" in str(msg) for msg in self.call.sent))
        self.assertFalse(any("A release" in str(msg) for msg in self.call.sent))
        g.observe("primary", output("Primary answer."))
        g.observe("primary", turn("done", "Primary answer.", role="assistant", tid="spoken"))
        g.observe("guest", turn("created", "Tek, back to you", 5000, 5800, tid="return"))
        g.flush_answers()
        self.assertTrue(any(isinstance(msg, dict) and msg.get("delegation_item_id") == "guest-id"
                            and "A release" in str(msg) for msg in self.call.sent))

    async def test_renewal_keeps_floor_requeues_delivery_and_rebases_new_addresses(self):
        g = self.guest
        g.observe("guest", turn("done", "Tek, what shipped?", 50_000, 51_000))
        self.call.protocol.seen.append("old-primary")
        live.Call.speak_for(self.call, "old-primary", "Primary answer.")
        g.transport_replaced("primary", 60_000)
        self.assertEqual(g.floor.owner, "guest")
        self.assertFalse(any("Primary answer" in str(msg) for msg in self.call.sent))

        g.protocol.seen.append("old-guest")
        g.floor.enqueue(Answer("guest", "old-guest", "Tek's answer."), g.config.name)
        g.flush_answers()
        self.assertEqual(self.call.sent[-1]["delegation_item_id"], "old-guest")
        g.protocol.seen.clear()
        g.transport_replaced("guest", 61_000)
        self.assertEqual(self.call.sent[-1]["type"], "session.context.append")
        self.assertIn("Tek's answer.", self.call.sent[-1]["content"][0]["text"])

        self.call.protocol.seen.clear()
        g.observe("primary", turn("done", "Hermes, back to you", 2_000, 3_000))
        g.flush_answers()
        self.assertEqual(g.floor.owner, "primary")
        self.assertEqual(self.call.sent[-1]["type"], "session.context.append")
        self.assertIn("Primary answer.", str(self.call.sent[-1]))

    async def test_guest_failure_announced_once_and_answer_preserved(self):
        g = self.guest
        g.observe("guest", turn("created", "Tek, what shipped?"))
        g.floor.enqueue(Answer("guest", "late", "A release."), "tek")
        g.failed("session_closed")
        g.failed("audio_ended")
        await asyncio.gather(*self.call.tasks)
        self.assertEqual((g.floor.owner, g.floor.reason), ("primary", "guest_failed"))
        self.assertEqual(sum("disconnected" in str(msg) for msg in self.call.sent), 1)
        g.observe("primary", output("Tek disconnected. I can keep going with you."))
        g.observe("primary", turn("done", "", role="assistant", tid="notice"))
        self.assertTrue(any("A release" in str(msg) for msg in self.call.sent))

    def test_config_reloads_and_keeps_legacy_grok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voices.json"
            with patch.dict(live.os.environ, {"Z0LIVE_VOICES_FILE": str(path), "Z0LIVE_GUEST": ""}):
                path.write_text('{"guest":"grok"}')
                self.assertEqual(guest_voice.configured().engine, "grok")
                path.write_text('{"guest":{"engine":"gpt-live","profile":"tek","name":"tek","voice":"ember"}}')
                self.assertEqual(guest_voice.configured(), self.guest.config)
                path.write_text('{"guest":{"engine":"gpt-live","profile":"../poteto","name":"tek","voice":"ember"}}')
                self.assertIsNone(guest_voice.configured())
