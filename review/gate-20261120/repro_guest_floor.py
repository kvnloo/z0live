"""Review-gate repros for fix/guest-floor (a70a58a). Run from voice-adapter/ of the head under test:

    Z0LIVE_DESKTOP=$PWD/../desktop env -u PYTHONPATH <venv>/python -m unittest discover -s <this dir> -p 'repro_guest_floor.py'

Each test states the caller-visible behaviour the S-tier bar expects. A failure is the finding.
"""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

import guest_voice
from guest_floor import Address, Addressing
import mobile_live as live
from test_gpt_guest import PrimaryCall, output, turn
from test_guest_voice import frame


class Clock:
    """mobile_live's view of time only; the asyncio loop keeps the real clock."""

    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class GuestCall(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        patcher = patch.object(live, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.call = PrimaryCall()
        self.guest = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
        self.addAsyncCleanup(self.guest.close)
        self.guest.message(json.dumps({"type": "session.started"}))
        self.guest.send = self.call.sent.append
        self.call.guest = self.guest

    def sends_of(self, text):
        # Only speakable sends make the voice say the answer; developer notes to the other leg are silent.
        return sum(text in str(msg) for msg in self.call.sent
                   if isinstance(msg, dict) and msg.get("channel") == "speakable")


class RedeliveryTest(GuestCall):
    async def test_answer_spoken_for_longer_than_15s_is_not_sent_twice(self):
        # A Hermes answer of a few hundred words takes > 15 s to speak. The caller must hear it once.
        g, answer = self.guest, "Primary answer about the deploy."
        self.call.protocol.seen.append("p1")
        live.Call.speak_for(self.call, "p1", answer)
        self.assertEqual(self.sends_of(answer), 1)
        self.clock.now = 101  # the primary starts speaking the answer
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output(answer))
        self.clock.now = 115.5  # the 15 s delivery timer fires while the reply is still playing
        g.flush_answers()
        self.clock.now = 121  # the reply finishes normally
        g.observe("primary", turn("done", answer, role="assistant", tid="r1"))
        self.assertEqual(self.sends_of(answer), 1, "answer was re-sent and will be spoken a second time")

    async def test_control_short_reply_is_sent_once(self):
        # Same flow with the reply done before 15 s. Passing here isolates the 15 s timer as the cause.
        g, answer = self.guest, "Primary answer about the deploy."
        self.call.protocol.seen.append("p1")
        live.Call.speak_for(self.call, "p1", answer)
        self.clock.now = 101
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output(answer))
        self.clock.now = 110
        g.observe("primary", turn("done", answer, role="assistant", tid="r1"))
        self.clock.now = 130
        g.flush_answers()
        self.assertEqual(self.sends_of(answer), 1)

    async def test_unspoken_answer_is_not_resent_forever(self):
        # The model may decline to speak a speakable item. Over 2 minutes it should not be re-sent 8 times.
        answer = "Primary answer about the deploy."
        self.call.protocol.seen.append("p1")
        live.Call.speak_for(self.call, "p1", answer)
        for second in range(115, 221, 15):
            self.clock.now = second + 0.01
            self.guest.flush_answers()
        self.assertLessEqual(self.sends_of(answer), 2, f"sent {self.sends_of(answer)} times in 2 minutes")


class AddressingTest(unittest.TestCase):
    def setUp(self):
        self.parse = Addressing(("tek", "teknium"))

    def test_naming_tek_then_saying_back_to_you_addresses_tek(self):
        # Their own test uses "Tek, back to you" (non-final). The final transcript must agree.
        self.assertIs(self.parse("Tek, back to you", final=True), Address.GUEST)

    def test_naming_tek_with_a_topic_handoff_still_addresses_tek(self):
        self.assertIs(self.parse("Tek, go back to the first point", final=True), Address.GUEST)

    def test_topic_phrase_is_not_a_redirect(self):
        # No name at all: must not move the floor.
        self.assertIsNone(self.parse("can we go back to the budget", final=True))


class FloorIntegrationTest(GuestCall):
    async def test_final_transcript_keeps_tek_on_the_floor(self):
        g = self.guest
        g.observe("primary", turn("created", "Tek, back to you", 5000, 5800, tid="u1"))
        self.assertEqual(g.floor.owner, "guest")
        g.observe("primary", turn("done", "Tek, back to you", 5000, 5800, tid="u1"))
        self.assertEqual((g.floor.owner, g.floor.reason), ("guest", "guest_addressed"))


class ReAddressTest(GuestCall):
    async def test_readdressing_tek_after_20s_keeps_tek_audible(self):
        # Advisor finding, verified here. Floor.change() is a no-op when Tek already owns the floor, so `since`
        # keeps the first grab time while user_turn() clears replied_at; expire() then measures from the first grab.
        g = self.guest
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000, tid="u1"))
        self.assertEqual(g.floor.owner, "guest")
        self.clock.now = 101
        g.observe("guest", turn("created", "", role="assistant", tid="r1"))
        g.observe("guest", output("A release."))
        self.clock.now = 118
        g.observe("guest", turn("done", "A release.", role="assistant", tid="r1"))
        self.clock.now = 122  # 4 s after Tek finished, 22 s after the first grab
        g.observe("primary", turn("created", "Tek, and what about tomorrow?", 22000, 23500, tid="u2"))
        g.observe("primary", turn("done", "Tek, and what about tomorrow?", 22000, 23500, tid="u2"))
        g.flush_answers()  # any guest event does this while Tek owns the floor
        self.assertEqual(g.floor.owner, "guest", f"floor moved: {g.floor.reason}")
        self.clock.now = 123
        self.assertTrue(g.observe("guest", output("Tomorrow is the review.")), "Tek's answer is muted")

    async def test_control_readdress_before_20s(self):
        g = self.guest
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000, tid="u1"))
        self.clock.now = 101
        g.observe("guest", turn("created", "", role="assistant", tid="r1"))
        g.observe("guest", output("A release."))
        self.clock.now = 110
        g.observe("guest", turn("done", "A release.", role="assistant", tid="r1"))
        self.clock.now = 114
        g.observe("primary", turn("created", "Tek, and what about tomorrow?", 14000, 15500, tid="u2"))
        g.observe("primary", turn("done", "Tek, and what about tomorrow?", 14000, 15500, tid="u2"))
        g.flush_answers()
        self.assertEqual(g.floor.owner, "guest")


class ShortBargeInTest(GuestCall):
    async def test_caller_no_cuts_a_reply_that_contains_know(self):
        # Advisor finding, verified here. interrupt() rejects caller words found anywhere in last_output as a
        # character substring, with no length floor: "no" is inside "know" and "now".
        g = self.guest
        self.assertTrue(g.observe("primary", output("I know the answer now, here it is")))
        self.call.output_loud_at = self.clock.now
        g.observe("primary", turn("created", "No", 1000, 1600, tid="b1"))
        self.assertFalse(g.observe("primary", output(" and more words")), "barge-in ignored")

    async def test_control_caller_no_cuts_a_reply_without_those_letters(self):
        g = self.guest
        self.assertTrue(g.observe("primary", output("Here it is")))
        self.call.output_loud_at = self.clock.now
        g.observe("primary", turn("created", "No", 1000, 1600, tid="b1"))
        self.assertFalse(g.observe("primary", output(" and more words")))


class ReplyTailTest(GuestCall):
    async def test_primary_audio_still_playing_after_turn_done_is_not_muted(self):
        # GPT-Live sends turn.done when generation ends; WebRTC audio keeps arriving after it (base traces without a
        # guest: p50 0.44-0.54 s, max 5.6 s, 9 of 53 replies > 1 s). The caller must hear the end of the sentence.
        g = self.guest
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        self.assertTrue(g.observe("primary", output("The answer is forty two.")))
        self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "fixture: reply audio passes while open")
        g.observe("primary", turn("done", "The answer is forty two.", role="assistant", tid="r1"))
        self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "reply tail was muted at turn.done")


class PrimaryDelegationTest(unittest.IsolatedAsyncioTestCase):
    async def test_primary_hermes_does_not_act_on_a_turn_addressed_to_tek(self):
        # 296d018 skipped host_delegate while the guest held the turn (mobile_live.py:944 `if not guest_turn`).
        # a70a58a removed that gate, leaving only the primary_note prompt ("do not delegate").
        config = guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek")
        with patch.object(live.guest_voice, "configured", return_value=config):
            call = live.Call(live.Manager(None), profile="")
        self.addAsyncCleanup(call.close, "test")
        ran = []

        async def host_delegate(did, request):
            ran.append(request)

        call.host_delegate = host_delegate
        call.heard_user = True
        call.guest.floor.available = True  # guest session.started
        call.message(json.dumps(turn("done", "Tek, delete the old release branch")), True)
        self.assertEqual(call.guest.floor.owner, "guest")
        call.message(json.dumps({"type": "delegation.created", "item": {
            "id": "d1", "target": "client", "content": [{"type": "input_text", "text": "delete the old branch"}]}}), True)
        await asyncio.sleep(0)
        self.assertEqual(ran, [], "Kevin's Hermes ran a request addressed to Tek")


if __name__ == "__main__":
    unittest.main()
