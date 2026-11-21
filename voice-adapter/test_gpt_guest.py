"""Two GPT-Live legs share the phone's audio and transcript floor."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web

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
            ("Tek, wait. Hermes, you take this.", Address.OTHER), ("Back to Hermes", Address.OTHER),
            ("Hermes, over to Tek", Address.GUEST),
            # A handoff moves the floor only to a known voice, never past a direct address to Tek in the same turn.
            ("Tek, back to you", Address.GUEST), ("Tek, go back to the first point", Address.GUEST),
            ("can we go back to the budget", None), ("Back to Julia", None),
        ]:
            with self.subTest(text=text):
                self.assertIs(parse(text, final=True), expected)
        self.assertIsNone(parse("Hey Tek", final=False))

    def test_each_voice_keeps_only_the_words_addressed_to_it(self):
        # A silent leg keeps one user turn open across both exchanges (11-21 two-voice probes).
        parse = Addressing(("tek", "teknium"))
        merged = "Tek, what did you work on today Thanks, Tek—Hermes, back to you. That's all for now, thanks"
        self.assertEqual(parse.own_words(merged, guest=False, lead=True),
                         "Hermes, back to you. That's all for now, thanks")
        self.assertEqual(parse.own_words("Hey, can you check the garden? Tek, what did you work on today?",
                                         guest=True, lead=False), "Tek, what did you work on today?")
        self.assertEqual(parse.own_words("Tek, what did you work on today?", guest=False, lead=True), "")
        self.assertEqual(parse.own_words("When is the boat inspection?", guest=False, lead=True),
                         "When is the boat inspection?")
        self.assertEqual(parse.own_words("one more detail", guest=True, lead=True), "one more detail")
        self.assertEqual(parse.own_words("Can you check the garden? Over to Tek", guest=False, lead=True),
                         "Can you check the garden? Over to")

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

    async def test_tek_back_to_you_keeps_tek_on_the_floor(self):
        g = self.guest
        g.observe("primary", turn("created", "Tek, back to you", 5000, 5800, tid="u1"))
        g.observe("primary", turn("done", "Tek, back to you", 5000, 5800, tid="u1"))
        self.assertEqual((g.floor.owner, g.floor.reason), ("guest", "guest_addressed"))

    async def test_caller_no_cuts_a_reply_that_contains_know(self):
        g = self.guest
        self.assertTrue(g.observe("primary", output("I know the answer now, here it is")))
        self.call.output_loud_at = live.time.monotonic()
        g.observe("primary", turn("created", "No", 1000, 1600, tid="b1"))
        self.assertFalse(g.observe("primary", output(" and more words")), "barge-in ignored")

    async def test_reply_open_at_renewal_does_not_mute_the_new_session(self):
        g = self.guest
        g.observe("guest", turn("done", "Tek, what shipped?", 1000, 2000))
        self.assertFalse(g.observe("primary", output("Muted, Tek has the floor.")))
        g.observe("primary", turn("done", "Hermes, your turn", 3000, 4000, tid="redirect"))
        g.transport_replaced("primary", 0)  # the retired session never sends turn.done for its open reply
        self.assertTrue(g.observe("primary", output("New session reply.")))

    async def test_guest_delegation_runs_on_tek_profile_only_while_tek_has_the_floor(self):
        received = []

        async def completion(request):
            session = request.headers["X-Hermes-Session-Id"]
            received.append((request.headers["Authorization"], session, (await request.json())["messages"][-1]["content"]))
            return web.json_response({"choices": [{"message": {"content": "A release."}}]},
                                     headers={"X-Hermes-Session-Id": session})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with tempfile.TemporaryDirectory() as home:
            keys = Path(home) / ".config/z0live/guest-hosts"
            keys.mkdir(parents=True)
            (keys / "tek.key").write_text("tek-key")
            (keys / "poteto.key").write_text("primary-key")
            with patch.object(Path, "home", return_value=Path(home)), patch.dict(
                    live.os.environ, {"Z0LIVE_GUEST_HOST_PORT": str(runner.addresses[0][1])}):
                async with ClientSession() as http:
                    self.call.manager = SimpleNamespace(http=http)
                    for did, text in (("unaddressed", "What is this?"), ("addressed", "Tek, what shipped?")):
                        self.guest.message(json.dumps(turn("created", "", tid=did)))
                        self.guest.message(json.dumps(turn("done", text, tid=did)))
                        self.guest.message(json.dumps({"type": "delegation.created", "item": {
                            "id": did, "target": "client", "content": [{"text": text}]}}))
                        await asyncio.gather(*self.call.tasks)
        self.assertEqual(received, [("Bearer tek-key", "z0live-guest-test-call", "Tek, what shipped?")])
        [answer] = [msg for msg in self.call.sent if isinstance(msg, dict) and msg.get("delegation_item_id")]
        self.assertEqual(answer["delegation_item_id"], "addressed")
        self.assertIn("A release.", answer["content"][0]["text"])


class Clock:
    """mobile_live's view of time only; the asyncio loop keeps the real clock."""

    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class TimedGuestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.enterContext(patch.object(live, "time", self.clock))
        self.call = PrimaryCall()
        self.guest = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
        self.addAsyncCleanup(self.guest.close)
        self.guest.message(json.dumps({"type": "session.started"}))
        self.guest.send = self.call.sent.append
        self.call.guest = self.guest

    def at(self, now):
        self.clock.now = now

    def speakable(self, text):
        """How many times the voice was handed this text to speak (developer notes are silent)."""
        return sum(text in str(msg) for msg in self.call.sent if isinstance(msg, dict) and msg.get("channel") == "speakable")

    def speak(self, did, text, **kw):
        self.call.protocol.seen.append(did)
        live.Call.speak_for(self.call, did, text, **kw)


class AnswerDeliveryTest(TimedGuestTest):
    async def test_answer_spoken_for_longer_than_15s_is_sent_once(self):
        # 12:10 call: an answer still being spoken 15 s after its send was sent again, and the caller heard it twice.
        g, answer = self.guest, "Primary answer about the deploy."
        self.speak("p1", answer)
        self.at(101)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output(answer))
        self.at(115.5)
        g.flush_answers()
        self.at(121)
        g.observe("primary", turn("done", answer, role="assistant", tid="r1"))
        self.at(140)
        g.flush_answers()
        self.assertEqual(self.speakable(answer), 1)

    async def test_answer_the_voice_never_speaks_is_sent_twice_then_dropped(self):
        self.speak("p1", "Primary answer about the deploy.")
        for second in range(115, 221, 15):
            self.at(second + 0.01)
            self.guest.flush_answers()
        self.assertEqual(self.speakable("Primary answer about the deploy."), 2)
        self.assertIn("floor.answer_dropped", self.call.traces)

    async def test_answer_on_the_floor_goes_out_while_a_reply_plays_and_only_once(self):
        # 12:10 call: Hermes answers waited 12 to 25 s behind replies already playing on the same floor.
        g = self.guest
        self.speak("p1", "Lane guess.")
        self.at(101)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Lane guess."))
        self.at(102)
        self.speak("p1", "Hermes answer.")
        self.assertEqual(self.speakable("Hermes answer."), 1, "queued behind the reply")
        self.at(120)  # the lane reply is still playing: the Hermes answer waits for the voice, not the clock
        g.flush_answers()
        self.at(125)
        g.observe("primary", turn("done", "Lane guess.", role="assistant", tid="r1"))
        self.at(125.5)
        g.observe("primary", turn("created", "", role="assistant", tid="r2"))
        self.at(145)
        g.flush_answers()
        self.assertEqual(self.speakable("Hermes answer."), 1)

    async def test_answer_merged_into_the_reply_already_playing_is_not_resent(self):
        # 192702 at 698.7 s: the answer landed mid-reply, the voice folded it in, and 15 s after that reply ended it
        # was sent again and spoken again ("Quick correction, actually nothing new has started").
        g = self.guest
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Four of the five garden beds are planted."))
        self.at(101)
        self.speak("p1", "Nothing new has started. I need your budget cap and the shed key first.")
        g.observe("primary", output(" I need your budget cap and the shed key."))
        self.at(110)
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        for second in (113, 126, 140):
            self.at(second)
            g.flush_answers()
        self.assertEqual(self.speakable("budget cap"), 1)
        self.assertIn("floor.answer_spoken", self.call.traces)

    async def test_answer_a_reply_never_says_is_resent_once_the_voice_is_quiet(self):
        # 205504 at 240.1 s: a reply started after the send ("Not yet", "Checking now") and that alone marked the
        # answer delivered. The voice never said it.
        g = self.guest
        self.speak("p1", "I couldn't repaint the fence because the paint store is closed.")
        self.at(100.6)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Not yet, checking now."))
        self.at(102)
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        self.at(103)
        g.flush_answers()
        self.assertEqual(self.speakable("paint store"), 1, "the voice just stopped: it may still say it")
        self.at(102 + live.answer_delivery.SETTLE_SECONDS)
        g.flush_answers()
        self.assertEqual(self.speakable("paint store"), 2)
        self.assertIn("floor.answer_resent", self.call.traces)

    async def test_one_delegations_pieces_resend_as_one_message_without_the_spoken_piece(self):
        g = self.guest
        self.speak("p1", "The garden has two planted beds.")
        self.speak("p1", "The tomato seedlings arrive Thursday from the nursery.")
        self.at(100.6)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Two beds in the garden are planted."))
        self.at(103)
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        self.at(110)
        before = len(self.call.sent)
        g.flush_answers()
        [resend] = [m for m in self.call.sent[before:] if isinstance(m, dict) and m.get("channel") == "speakable"]
        self.assertIn("tomato seedlings", str(resend))
        self.assertNotIn("planted beds", str(resend))

    async def test_answer_the_caller_cut_off_is_not_resent(self):
        g = self.guest
        self.speak("p1", "Here are two photos of the porch railing and the cracked steps.")
        self.at(100.6)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Here are two photos"))
        self.call.output_loud_at = 100.6
        g.observe("primary", turn("created", "Wait, different question", 1000, 1800, tid="barge"))
        self.assertIn("floor.barge_in", self.call.traces)
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        self.at(120)
        g.flush_answers()
        self.assertEqual(self.speakable("porch railing"), 1)

    async def test_lane_guesses_waiting_for_the_floor_are_dropped_once_stale(self):
        g = self.guest
        g.observe("guest", turn("done", "Tek, what shipped?", 1000, 2000))
        self.call.latest_did = "p1"
        self.speak("p0", "Guess for an older question.", lane=True)
        self.speak("p1", "Guess for this question.", lane=True)
        self.speak("p1", "Hermes answer for this question.")
        self.assertEqual(self.call.sent, [], "fixture: Tek has the floor")
        g.observe("primary", turn("done", "Hermes, your turn", 3000, 4000, tid="u2"))
        g.flush_answers()  # Call.message flushes after every provider event while primary answers wait
        self.assertEqual(self.speakable("Hermes answer for this question."), 1)
        self.assertEqual(self.speakable("Guess for"), 0)


class FloorTimingTest(TimedGuestTest):
    async def test_readdressing_tek_after_20s_keeps_tek_audible(self):
        g = self.guest
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000, tid="u1"))
        self.at(101)
        g.observe("guest", turn("created", "", role="assistant", tid="r1"))
        g.observe("guest", output("A release."))
        self.at(118)
        g.observe("guest", turn("done", "A release.", role="assistant", tid="r1"))
        self.at(122)  # 22 s after Tek first got the floor
        g.observe("primary", turn("created", "Tek, and what about tomorrow?", 22000, 23500, tid="u2"))
        g.observe("primary", turn("done", "Tek, and what about tomorrow?", 22000, 23500, tid="u2"))
        g.flush_answers()
        self.at(123)
        self.assertTrue(g.observe("guest", output("Tomorrow is the review.")), f"muted: floor {g.floor.reason}")

    async def test_reply_audio_plays_after_turn_done_until_the_output_goes_quiet(self):
        g = self.guest
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("The answer is forty two."))
        self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "fixture: reply audio passes while open")
        g.observe("primary", turn("done", "The answer is forty two.", role="assistant", tid="r1"))
        self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "tail muted at turn.done")
        self.call.output_loud_at = 100.2  # the phone still hears the sentence end
        self.at(100.45)
        self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "tail muted while still loud")
        self.at(100.55)  # quiet for longer than LEVEL_OFF_SECONDS: anything after is not this reply
        self.assertFalse(g.output(frame()).to_ndarray().any())


class PrimaryDelegationTest(unittest.IsolatedAsyncioTestCase):
    async def delegate(self, said):
        config = guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek")
        with patch.dict(live.os.environ, {"HERMES_VOICE_ENGINE": "gpt-live"}), \
                patch.object(live.guest_voice, "configured", return_value=config):
            call = live.Call(live.Manager(None), profile="")
        self.addAsyncCleanup(call.close, "test")
        ran = []

        async def host_delegate(did, request):
            ran.append(request)

        call.host_delegate, call.relay = host_delegate, None  # no lanes: nothing leaves the process
        call.heard_user = True
        call.guest.floor.available = True  # guest session.started
        call.message(json.dumps(turn("done", said)), True)
        call.message(json.dumps({"type": "delegation.created", "item": {
            "id": "d1", "target": "client", "content": [{"type": "input_text", "text": said}]}}), True)
        await asyncio.sleep(0)
        return ran

    @patch.object(live, "HOST_DELEGATION", True)
    async def test_kevins_hermes_never_runs_a_request_addressed_to_tek(self):
        self.assertEqual(await self.delegate("Tek, delete the old release branch"), [])

    @patch.object(live, "HOST_DELEGATION", True)
    async def test_words_said_to_tek_never_join_the_next_request_to_hermes(self):
        # 11-21 two-voice probe: after "Tek, what did you work on today?" and Tek's answer, "Hermes, back to you"
        # delegated "Tek, what did you work on today Thanks, Tek. Hermes, back to you" to Kevin's Hermes.
        config = guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek")
        with patch.dict(live.os.environ, {"HERMES_VOICE_ENGINE": "gpt-live"}), \
                patch.object(live.guest_voice, "configured", return_value=config):
            call = live.Call(live.Manager(None), profile="")
        self.addAsyncCleanup(call.close, "test")
        ran = []

        async def host_delegate(did, request):
            ran.append(request)

        call.host_delegate, call.relay, call.heard_user = host_delegate, None, True
        call.guest.floor.available = True
        guest_ran = []
        call.guest.delegate = lambda did, request: guest_ran.append(request) or asyncio.sleep(0)
        primary = lambda event: call.message(json.dumps(event), True)  # noqa: E731
        guest = lambda event: call.guest.message(json.dumps(event))  # noqa: E731

        def delta(text, start, end):
            return {"type": "turn.delta", "turn_id": "p1", "delta": text, "start_ms": start, "end_ms": end}

        # Tek's leg hears the caller's earlier question to Hermes in the same turn as the address to Tek.
        guest(turn("created", "", 1000, 1200, tid="g1"))
        guest(turn("done", "Hey, can you check the garden project? Tek, what did you work on today?", 1000, 6000,
                   tid="g1"))
        guest({"type": "delegation.created", "item": {"id": "gd1", "target": "client", "content": [
            {"type": "input_text", "text": "what did you work on today"}]}})
        # The primary stays silent while Tek talks, so its leg keeps ONE user turn open across both exchanges.
        primary(turn("created", "", 1000, 1200, tid="p1"))
        primary(delta("Tek, what did you work on today?", 3500, 6000))
        guest(turn("created", "", 60000, 60200, tid="g2"))
        guest(turn("done", "Thanks, Tek. Hermes, back to you. When is the boat inspection?", 60000, 64000, tid="g2"))
        primary(delta(" Thanks, Tek. Hermes, back to you. When is the boat inspection?", 60000, 64000))
        primary(turn("done", "Tek, what did you work on today? Thanks, Tek. Hermes, back to you. When is the boat "
                             "inspection?", 3500, 64000, tid="p1"))
        primary({"type": "delegation.created", "item": {"id": "d1", "target": "client", "content": [
            {"type": "input_text", "text": "Thanks, Tek. Hermes, back to you. When is the boat inspection"}]}})
        await asyncio.sleep(0)
        self.assertEqual(len(ran), 1, f"floor {call.guest.floor.owner}")
        self.assertNotIn("work on today", ran[0])
        self.assertNotIn("Tek", ran[0], "the provider's delegation segment carries the words to Tek too")
        self.assertIn("boat inspection", ran[0])
        self.assertEqual(len(guest_ran), 1)
        self.assertNotIn("garden", guest_ran[0])
        self.assertIn("work on today", guest_ran[0])

    @patch.object(live, "HOST_DELEGATION", True)
    async def test_kevins_hermes_runs_a_request_addressed_to_it(self):
        self.assertEqual(await self.delegate("Delete the old release branch"), ["Delete the old release branch"])
