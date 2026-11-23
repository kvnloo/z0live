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

    def test_with_people_on_the_call_a_bot_gets_the_floor_by_name_or_a_follow_up_request(self):
        # 11-22 film call, take 7: Kevin and a friend at one phone; their talk to each other ran Hermes and got speech.
        floor = Floor(0, lambda *a, **k: None, available=True, people=2)
        floor.change("people", "people_on_call", 0)
        floor.user_turn(None, "Is the glass theme on your phone yet?", 1, 2, final=True)
        self.assertEqual(floor.owner, "people")
        floor.user_turn(Address.OTHER, "Hermes, what's on my phone?", 3, 4, final=True)
        self.assertEqual((floor.owner, floor.reason), ("primary", "redirected"))
        floor.reply_started("primary", 5)
        floor.user_turn(None, "Get all of it onto my S25", 6, 7, final=True)  # barge-in with the next request
        self.assertEqual(floor.owner, "primary")
        floor.reply_finished("primary", 9, 8)
        floor.user_turn(None, "Get it onto the tablet too", 10, 11, final=True)
        self.assertEqual(floor.owner, "primary")
        floor.user_turn(None, "Okay, nice. That's the one I kept hitting", 12, 13, final=True)  # a remark, not a request
        self.assertEqual((floor.owner, floor.reason), ("people", "people_talk"))
        floor.change("primary", "redirected", 12.5)
        floor.reply_started("primary", 12.6)
        # Take 15: line 5 opened with the "?" that ended line 4 ("... in PR 28?"), and Tek's Hermes ran for it.
        floor.user_turn(None, "? Okay nice. That's the one I kept hitting", 12.7, 13, final=True)
        self.assertEqual((floor.owner, floor.reason), ("people", "people_talk"))
        floor.user_turn(Address.GUEST, "Tek, what caused the crash?", 14, 15, final=True)
        floor.reply_finished("guest", 20, 16)
        floor.user_turn(None, "And can you check the fix too?", 45, 46, final=True)  # too long after the reply
        self.assertEqual(floor.owner, "people")
        floor.user_turn(None, "Mm-hm", 47, 48, final=True)
        self.assertEqual(floor.owner, "people")
        self.assertFalse(floor.answer_ready(lambda leg: False, 50))
        floor.enqueue(Answer("primary", "d1", "It is already running."), "tek")
        self.assertTrue(floor.answer_ready(lambda leg: False, 50))
        self.assertEqual((floor.owner, floor.reason), ("primary", "answer_ready"))

    def test_with_people_on_the_call_a_request_that_cut_off_the_bot_stays_with_it(self):
        # Take 8: Kevin began his next request as Hermes finished; Hermes then began "Actually," and Kevin's barge-in
        # cut it. Measured against that last reply (begun after him), his request was taken for people talk and
        # never reached Hermes.
        floor = Floor(0, lambda *a, **k: None, available=True, people=2)
        floor.user_turn(Address.OTHER, "Hermes, what's on my phone versus staging?", 1, 2, final=True)
        floor.reply_started("primary", 3)
        floor.reply_finished("primary", 17, 3)
        floor.reply_started("primary", 23)  # "Actually," while Kevin (since 20) is already talking
        floor.reply_finished("primary", 24, 23)  # the barge-in
        floor.user_turn(None, "Yeah. Merged isn't done. Get all of it onto my S25", 20, 28, final=True)
        self.assertEqual(floor.owner, "primary")
        floor.user_turn(None, "Okay, I think it's still last night's build", 50, 52, final=True)
        self.assertEqual(floor.owner, "people")

    def test_with_people_on_the_call_an_idle_guest_leaves_the_floor_to_the_people(self):
        floor = Floor(0, lambda *a, **k: None, available=True, people=2)
        floor.user_turn(Address.GUEST, "Tek, what", 1, 1, final=True)
        floor.reply_finished("guest", 15, 12)
        floor.expire(35)
        self.assertEqual((floor.owner, floor.reason), ("people", "guest_idle"))

    def test_silence_starts_after_the_reply(self):
        floor = Floor(0, lambda *a, **k: None, available=True)
        floor.user_turn(Address.GUEST, "Tek, what", 1, 1, final=True)
        floor.reply_finished("guest", 15, 12)
        floor.expire(34.99)
        self.assertEqual(floor.owner, "guest")
        floor.expire(35)
        self.assertEqual((floor.owner, floor.reason), ("primary", "guest_idle"))


async def working(*_, **__):
    await asyncio.Event().wait()


def utterance_ends(guest):
    """The caller's mic and transcript have been quiet for EOU_SECONDS: the floor's one decision on the utterance."""
    guest.user_heard_at -= live.EOU_SECONDS + 0.1
    guest.end_of_utterance()


class PrimaryCall(FakeCall):
    id = "test-call"
    provider_ready = heard_user = True
    input_loud_at = output_loud_at = float("-inf")

    def __init__(self):
        super().__init__()
        self.protocol = live.Protocol(host_delegation=True)
        self.tasks = []
        self.manager = type("Manager", (), {"http": None})()
        self.delegated = []  # (did, request) the floor handed to Kevin's Hermes

    async def host_delegate(self, did, request):
        self.delegated.append((did, request))

    def spawn(self, coro):
        self.tasks.append(asyncio.create_task(coro))

    def guest_note(self, text):
        self.sent.append(text)

    def host_context(self):
        return ""  # a call opened from no chat


class GPTGuestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.call = PrimaryCall()
        self.guest = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
        self.addAsyncCleanup(self.guest.close)
        self.guest.message(json.dumps({"type": "session.started"}))
        self.guest.send = self.call.sent.append
        self.call.guest = self.call.duplex = self.guest

    async def test_guest_persona_asks_for_a_neutral_handoff(self):
        # Take 9: Tek's leg said "Understood, I'll handle it end-to-end and show results when it's done."
        self.assertIn('"Let me check"', self.guest.persona)
        self.assertIn("never promise new work, say you are taking it on or handling it", self.guest.persona)
        self.assertIn("promise receipts or a follow-up", self.guest.persona)

    async def test_suppressed_reply_never_leaks_audio_text_or_context(self):
        self.guest.observe("primary", turn("done", "Tek, what shipped?"))
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

    async def test_a_reply_the_floor_muted_is_heard_once_the_floor_passes_back_for_its_answer(self):
        # Take 12, line 3: Hermes's answer waited while the people talked. The primary's reply opened (as the floor
        # left it, or before the floor came back), and when answer_ready gave the primary the floor the reply stayed
        # muted: the voice read the whole answer into silence and the ledger dropped it as unspoken.
        for opened in ("under the people", "then cut by people talk"):
            with self.subTest(opened=opened):
                g = live.GPTGuest(self.call, None)
                g.set_people(2)
                if opened != "under the people":
                    g.floor.change("primary", "answer_ready", live.time.monotonic())
                g.observe("primary", turn("created", "", role="assistant", tid="r1"))
                g.observe("primary", output("That's"))
                if opened != "under the people":
                    g.floor.change("people", "people_talk", live.time.monotonic())
                self.assertFalse(g.observe("primary", output(" already")), "muted while the people talk")
                g.floor.answers["primary"].append(Answer("primary", "d1", "Your laptop is on build 30812."))
                g.floor.answer_ready(lambda leg: bool(g.floor.answers[leg]), live.time.monotonic())
                self.assertTrue(g.observe("primary", output(" running. Your laptop is on build 30812.")),
                                "the reply carrying the answer reaches the phone")
                self.assertTrue((g.output(frame()).to_ndarray() == 7).all(), "and its audio plays")

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

    async def test_the_other_voice_hears_only_the_gist_of_what_tek_said(self):
        # 11-21 deployed two-voice probe: with Tek's whole reply as context, "Hermes, back to you" made the primary
        # re-tell it ("Sure thing. Here's where we stand: ...").
        g = self.guest
        g.observe("primary", turn("done", "Tek, what did you do today?", 1000, 2000, tid="u1"))
        g.observe("guest", turn("created", "", role="assistant", tid="r1"))
        g.observe("guest", output("Mostly disk cleanup on the Studio. The biggest chunk is the backup copy. "
                                  "Two things need your call: delete it, and the timers."))
        g.observe("guest", turn("done", "", role="assistant", tid="r1"))
        [note] = [n for n in self.call.sent if isinstance(n, str) and "tek said" in n]
        self.assertIn("Mostly disk cleanup on the Studio.", note)
        self.assertNotIn("backup copy", note)
        self.assertNotIn("timers", note)

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
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000))
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
            self.assertEqual(host.await_args.args[1:], ("tek", "z0live-guest-test-call", "What shipped?", ""))
        self.assertTrue(any("Primary answer" in str(msg) for msg in self.call.sent))
        self.assertFalse(any("A release" in str(msg) for msg in self.call.sent))
        g.observe("primary", output("Primary answer."))
        g.observe("primary", turn("done", "Primary answer.", role="assistant", tid="spoken"))
        g.observe("primary", turn("created", "Tek, back to you", 5000, 5800, tid="return"))
        g.flush_answers()
        self.assertTrue(any(isinstance(msg, dict) and msg.get("delegation_item_id") == "guest-id"
                            and "A release" in str(msg) for msg in self.call.sent))

    async def test_renewal_keeps_floor_requeues_delivery_and_takes_new_addresses(self):
        g = self.guest
        g.observe("primary", turn("done", "Tek, what shipped?", 50_000, 51_000))
        self.call.protocol.seen.append("old-primary")
        live.Call.speak_for(self.call, "old-primary", "Primary answer.")
        g.transport_replaced("primary")
        self.assertEqual(g.floor.owner, "guest")
        self.assertFalse(any("Primary answer" in str(msg) for msg in self.call.sent))

        g.protocol.seen.append("old-guest")
        g.floor.enqueue(Answer("guest", "old-guest", "Tek's answer."), g.config.name)
        g.flush_answers()
        self.assertEqual(self.call.sent[-1]["delegation_item_id"], "old-guest")
        g.protocol.seen.clear()
        g.transport_replaced("guest")
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
        g.observe("primary", turn("created", "Tek, what shipped?"))
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

    async def test_people_talk_is_neither_heard_nor_delegated_and_both_voices_are_told(self):
        g = self.guest
        g.set_people(2)
        self.assertEqual(g.floor.owner, "people")
        g.observe("primary", turn("created", "", 1000, 1200, tid="u1"))
        g.observe("primary", turn("done", "Is the glass theme on your phone yet?", 1000, 3000, tid="u1"))
        self.assertFalse(g.observe("primary", output("I'll check your screen quickly.")))
        utterance_ends(g)
        self.assertEqual(g.floor.owner, "people")
        self.assertFalse(g.output(frame()).to_ndarray().any())
        self.assertFalse(g.may_delegate("primary", "d1"), "the provider's own delegations never run")
        self.assertFalse(g.may_delegate("guest", "gd1"))
        g.observe("primary", turn("done", "", role="assistant", tid="hidden"))
        g.observe("primary", turn("done", "Hermes, what's on my phone?", 4000, 6000, tid="u2"))
        self.assertFalse(g.may_delegate("primary", "d2"))
        utterance_ends(g)
        await asyncio.gather(*self.call.tasks)
        self.assertEqual(self.call.delegated, [("d2", "Hermes, what's on my phone?")],
                         "the floor hands the words to Hermes itself, under the provider's delegation id")
        self.assertTrue(g.observe("primary", output("Checking.")))
        self.assertFalse(any("I'll check your screen" in str(msg) for msg in self.call.sent))

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
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000))
        self.assertFalse(g.observe("primary", output("Muted, Tek has the floor.")))
        g.observe("primary", turn("done", "Hermes, your turn", 3000, 4000, tid="redirect"))
        g.transport_replaced("primary")  # the retired session never sends turn.done for its open reply
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
                        self.guest.observe("primary", turn("done", text, tid="p-" + did))  # the floor's transcript
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
        # Tek's Hermes, still working on whatever the floor hands it
        self.tek = self.enterContext(patch.object(live, "ask_guest_host", AsyncMock(side_effect=working)))
        self.call = PrimaryCall()
        self.guest = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
        self.addAsyncCleanup(self.guest.close)
        self.guest.message(json.dumps({"type": "session.started"}))
        self.guest.send = self.call.sent.append
        self.call.guest = self.call.duplex = self.guest

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

    async def test_answer_the_caller_cut_off_with_his_next_request_is_not_resent(self):
        g = self.guest
        self.speak("p1", "Here are two photos of the porch railing and the cracked steps.")
        self.at(100.6)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("Here are two photos"))
        self.call.output_loud_at = 100.6
        g.observe("primary", turn("created", "Wait, different question", 1000, 1800, tid="barge"))
        self.assertIn("floor.barge_in", self.call.traces)
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        g.observe("primary", turn("done", "Wait, different question: is the mailbox fixed?", 1000, 3000, tid="barge"))
        self.at(120)
        g.flush_answers()
        g.floor.change("guest", "guest_addressed", 121)
        g.floor.change("primary", "guest_idle", 142)  # the floor coming back later does not revive it
        g.flush_answers()
        self.assertEqual(self.speakable("porch railing"), 1)
        self.assertIn("floor.answer_dropped", self.call.traces)

    # Take 13, line 3 (words changed): Hermes's answer had played one sentence when a friend said "Hey Tek, while
    # that runs, ..." over it. The answer was dropped as barged_in, and its receipts and its "I can't install
    # anything from this call" were never heard.
    LINE_3 = ("That's already running as card t_0a96e2de, so I won't start a second one. The receipts so far are a "
              "signed build and passing screenshot tests. I can't install anything from this call.")

    def cut_by_a_friend_asking_tek(self):
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.speak("d3", self.LINE_3)
        self.at(100.6)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("It's still running in card t_0a96e2de, so I won't start a second one."))
        self.at(104)
        self.call.output_loud_at = 104
        g.observe("primary", turn("created", "Hey Tek, while that runs, what caused the crash", 4000, 4800, tid="j1"))
        utterance_ends(g)
        self.assertEqual(g.floor.owner, "guest", "fixture: the friend's words gave Tek the floor")
        g.observe("primary", turn("done", "", role="assistant", tid="r1"))
        return g

    async def test_an_answer_cut_by_a_friend_asking_tek_comes_back_when_the_floor_does(self):
        g = self.cut_by_a_friend_asking_tek()
        self.assertEqual(self.speakable("from this call"), 1)
        g.floor.change("people", "people_talk", 106)
        self.at(108)  # the people's pause
        g.flush_answers()
        self.assertEqual(g.floor.owner, "primary")
        self.assertEqual(self.speakable("from this call"), 2, "the rest of the answer goes back to Hermes's voice")
        self.assertEqual(self.speakable("second one"), 1, "and only the rest")
        self.assertIn("floor.answer_resumed", self.call.traces)

    async def test_an_answer_cut_by_a_friend_is_superseded_by_a_new_request_to_the_same_voice(self):
        g = self.cut_by_a_friend_asking_tek()
        self.at(110)
        g.observe("primary", turn("done", "Hermes, what about the laptop?", 9000, 10000, tid="u2"))
        utterance_ends(g)
        g.flush_answers()
        self.assertEqual(g.floor.owner, "primary")
        self.assertEqual(self.speakable("from this call"), 1)

    async def test_an_answer_playing_when_the_floor_moves_away_is_cut_not_lost(self):
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.speak("d3", self.LINE_3)
        g.observe("primary", turn("created", "", role="assistant", tid="r1"))
        g.observe("primary", output("It's still running in card t_0a96e2de, so I won't start a second one."))
        g.floor.change("guest", "guest_addressed", 104)  # no barge-in: the output was not loud
        self.assertIn("floor.answer_cut", self.call.traces)
        g.floor.change("people", "people_talk", 106)
        self.at(108)
        g.flush_answers()
        self.assertEqual(self.speakable("from this call"), 2)

    async def test_a_rest_waits_for_the_callers_open_turn(self):
        # Take 16, 97.969 s: the rest went out while the caller's turn on that leg was still open (it closed at 98.778).
        g = self.cut_by_a_friend_asking_tek()
        self.at(105)
        g.observe("primary", turn("created", "Okay, nice, that's the one I kept hitting", 5000, 5800, tid="u5"))
        utterance_ends(g)
        self.assertEqual(g.floor.owner, "people")
        self.at(106)
        g.flush_answers()
        self.assertEqual(self.speakable("from this call"), 1, "not while the caller's turn is open")
        self.at(106.5)
        g.observe("primary", turn("done", "Okay, nice, that's the one I kept hitting", 5000, 6200, tid="u5"))
        self.at(108.1)
        g.flush_answers()
        self.assertEqual(g.floor.owner, "primary")
        self.assertEqual(self.speakable("from this call"), 2, "once it closed, in the caller's pause")

    async def test_teks_answer_cut_by_people_talk_comes_back_to_tek(self):
        # Take 13, line 4 (words changed): three words in, Tek's answer counted as spoken, and line 5 ("Okay,
        # nice") cut it at "the blur". Nothing more was said.
        g = self.guest
        g.set_people(2)
        g.observe("primary", turn("done", "Tek, why did the shed door stick?", 1000, 2000, tid="u1"))
        utterance_ends(g)
        g.floor.enqueue(Answer("guest", "g1", "The shed door stuck because the hinge pin rusted. The new pin went "
                                              "in on Tuesday. Oil it twice a year."), "Tek")
        g.flush_answers()
        self.at(101)
        g.observe("guest", turn("created", "", role="assistant", tid="r1"))
        g.observe("guest", output("The shed door stuck, the hinge pin rusted. The new"))
        self.assertNotIn("floor.answer_spoken", self.call.traces, "begun is not heard: the reply's end decides")
        self.at(103)
        self.call.output_loud_at = 103
        g.observe("primary", turn("created", "Okay, nice, that's good to know", 3000, 3800, tid="u2"))
        g.observe("primary", turn("done", "Okay, nice, that's good to know", 3000, 4200, tid="u2"))
        utterance_ends(g)
        self.assertEqual(g.floor.owner, "people")
        g.observe("guest", turn("done", "", role="assistant", tid="r1"))
        self.at(106)
        g.flush_answers()
        self.assertEqual(g.floor.owner, "guest")
        self.assertEqual(self.speakable("twice a year"), 2)
        self.assertEqual(self.speakable("hinge pin rusted"), 1)

    async def test_a_friend_saying_hey_tech_asks_tek(self):
        # Takes 9 and 14: both legs heard Jules's "Hey, Tek" as "Hey, Tech", the floor read it as the people talking
        # to each other, and Tek's Hermes was never asked about PR 28.
        for text, owner in [("Hey, Tech, while that runs, what actually caused the glass crash in PR 28", "guest"),
                            ("Tec, what caused it?", "guest"), ("Tech's, what caused it?", "guest"),
                            ("Hermes, is the tech stack on the laptop the same as staging?", "primary")]:
            with self.subTest(text=text):
                g = live.GPTGuest(self.call, guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek"))
                g.send = self.call.sent.append
                g.message(json.dumps({"type": "session.started"}))
                g.set_people(2)
                g.floor.change("primary", "redirected", 100)
                g.observe("primary", turn("done", text, 1000, 4000, tid="j1"))
                utterance_ends(g)
                self.assertEqual(g.floor.owner, owner)
                await g.close()

    async def test_an_answer_the_provider_cut_under_the_callers_words_comes_back_in_their_pause(self):
        # Take 14, line 5: the provider ended the resumed line-3 answer itself at "then verify" when Jules said
        # "Okay, nice". No barge-in fired ("Okay" is a backchannel, and the output had been quiet 0.26 s), nothing
        # was handed back, Jules's turn never closed, and the call was silent for 60 s.
        for order in ("turn.done first", "caller first"):
            with self.subTest(order=order):
                await self.asyncSetUp()
                g = self.guest
                g.set_people(2)
                g.floor.change("primary", "redirected", 100)
                self.speak("d3", self.LINE_3)
                self.at(100.6)
                g.observe("primary", turn("created", "", role="assistant", tid="r1"))
                g.observe("primary", output("It's still running in card t_0a96e2de, so I won't start a second one."))
                self.at(104)
                done = turn("done", "", 600, 4200, role="assistant", tid="r1")
                okay = turn("created", "Okay", 4000, 4200, tid="j5")  # began inside the reply, provider clock
                for event in ((done, okay) if order == "turn.done first" else (okay, done)):
                    g.observe("primary", event)
                self.assertIn("floor.answer_cut", self.call.traces)
                self.at(104.5)
                g.flush_answers()
                self.assertEqual(self.speakable("from this call"), 1, "not while the caller is still talking")
                self.at(106.6)
                utterance_ends(g)  # Jules's turn never closed: it ends on quiet
                g.flush_answers()
                self.assertEqual(self.speakable("from this call"), 2, "the rest comes back in the caller's pause")
                self.assertEqual(self.speakable("second one"), 1, "and only the rest")
                await g.close()

    async def test_a_reply_stopped_mid_sentence_hands_the_rest_back_and_one_that_ran_its_course_does_not(self):
        # The sim's provider stops a reply at a random word with nobody talking: the answer was credited as spoken
        # (answer_delivery's few words) and its rest never came back. Take 15 (144.7 and 150.9 s): replies that ended
        # on their own, on a sentence end with the mic quiet, were cut too, and their last sentence re-queued twice.
        for said, cut in (("It's still running in card t_0a96e2de, so I won't", True),
                          ("It's still running in card t_0a96e2de, so I won't start a second one.", False)):
            with self.subTest(said=said):
                await self.asyncSetUp()
                g = self.guest
                g.set_people(2)
                g.floor.change("primary", "redirected", 100)
                self.speak("d3", self.LINE_3)
                self.at(100.6)
                g.observe("primary", turn("created", "", role="assistant", tid="r1"))
                g.observe("primary", output(said))
                self.at(104)
                g.observe("primary", turn("done", "", 600, 4200, role="assistant", tid="r1"))
                g.observe("primary", turn("created", "Okay", 4400, 4600, tid="j5"))  # after the reply, not over it
                self.assertNotIn("floor.barge_in", self.call.traces)
                self.assertEqual("floor.answer_cut" in self.call.traces, cut)
                self.at(107)
                utterance_ends(g)
                g.flush_answers()
                self.assertEqual(self.speakable("from this call"), 2 if cut else 1, "the rest goes back if stopped")
                self.assertEqual(self.speakable("second one"), 1, "and only the rest")
                await g.close()

    async def test_lane_guesses_waiting_for_the_floor_are_dropped_once_stale(self):
        g = self.guest
        g.observe("primary", turn("done", "Tek, what shipped?", 1000, 2000))
        self.call.latest_did = "p1"
        self.speak("p0", "Guess for an older question.", lane=True)
        self.speak("p1", "Guess for this question.", lane=True)
        self.speak("p1", "Hermes answer for this question.")
        self.assertEqual(self.call.sent, [], "fixture: Tek has the floor")
        g.observe("primary", turn("done", "Hermes, your turn", 3000, 4000, tid="u2"))
        g.flush_answers()  # Call.message flushes after every provider event while primary answers wait
        self.assertEqual(self.speakable("Hermes answer for this question."), 1)
        self.assertEqual(self.speakable("Guess for"), 0)


class Take16FloorTest(TimedGuestTest):
    """Take 16, line 3 (words changed)."""

    ANSWER = ("Card t_0b17 has fixed the shed door since Monday, so I won't start a second job. Its steps are to oil "
              "the hinge, then check it every week.\n\n"
              "A correction: the pin is brass, not steel.\n\n"
              "I can only report from this call. The card keeps going on its own.")

    def said_by_a_reply(self, rid, text, start, end):
        g = self.guest
        self.at(start)
        g.observe("primary", turn("created", "", role="assistant", tid=rid))
        g.observe("primary", output(text))
        self.at(end)
        g.observe("primary", turn("done", "", int(start * 1000), int(end * 1000), role="assistant", tid=rid))

    async def test_a_paragraph_a_reply_skipped_comes_back_with_one_cue(self):
        # 98.8-121.2 s: the voice said paragraph 2 and a paraphrase of paragraph 3 and ran its course; paragraph 1
        # (card t_0a96e2de, "won't start a second run") was credited at answer_spoken 100.365 and never resent.
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.speak("d3", self.ANSWER)
        self.said_by_a_reply("r1", "Quick correction, the pin is brass, not steel. The card keeps going on its own.",
                             100.6, 110)
        self.assertIn("floor.answer_unsaid", self.call.traces)
        self.assertNotIn("floor.answer_cut", self.call.traces, "it ran its course: not a cut")
        self.at(112)
        g.flush_answers()
        self.assertEqual(self.speakable("t_0b17"), 2, "paragraph 1 goes back")
        self.assertEqual(self.speakable("brass"), 1, "and only paragraph 1")
        self.said_by_a_reply("r2", "The card is fixing the shed door. Its steps: oil the hinge every week.", 112.5, 115)
        self.at(117)
        g.flush_answers()
        self.assertEqual(self.speakable("t_0b17"), 3, "skipped again: the card's sentence once more (take 18)")
        self.assertEqual(self.speakable("hinge"), 2, "only the sentence naming the card")
        self.said_by_a_reply("r3", "Since Monday the card has had the shed door, so no second job.", 117.5, 120)
        self.at(126)
        g.flush_answers()
        self.assertEqual(self.speakable("t_0b17"), 3, "skipped a third time: dropped")
        self.assertEqual([m for m in self.call.sent if str(m).count("To finish what I was saying") > 1], [])

    async def test_a_reply_that_put_the_answer_in_its_own_words_is_heard(self):
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.speak("d3", "It was a startup-order bug. The app sets up the window before it is built, so the view "
                         "doesn't exist yet. The fix is merged, but it isn't on your S25 yet.")
        self.said_by_a_reply("r1", "In short, the app set up the window too early. The fix is merged, not on your S "
                                   "twenty-five yet.", 100.6, 106)
        self.assertIn("floor.answer_spoken", self.call.traces)
        self.assertNotIn("floor.answer_unsaid", self.call.traces)
        self.at(112)
        g.flush_answers()
        self.assertEqual(self.speakable("startup-order"), 1)

    async def test_an_answer_dropped_for_a_new_request_is_not_resent(self):
        # 51.974 s: floor.answer_dropped (new_request) for line 2's answer, then 16 ms later floor.answer_resent.
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.speak("d2", "Your laptop is on build four.")  # handed, never said
        g.floor.answers["primary"].append(Answer("primary", "d2", "Staging is ahead of it.", rest=True))
        self.at(101)
        g.observe("primary", turn("done", "Can you get it onto my laptop too?", 1000, 2000, tid="u3"))
        utterance_ends(g)
        self.assertIn("floor.answer_dropped", self.call.traces)
        self.at(110)
        g.flush_answers()
        self.assertEqual(self.speakable("build four"), 1)
        self.assertNotIn("floor.answer_resent", self.call.traces)

    async def test_the_last_word_of_a_request_heard_late_ends_that_request(self):
        # 52.836 s: line 3's last word "receipts" came 17 ms after the floor decided, became its own utterance, and
        # gave the floor to the people, muting the reply.
        for gap, owner in ((200, "primary"), (1400, "people")):
            with self.subTest(gap=gap):
                await self.asyncSetUp()
                g = self.guest
                g.set_people(2)
                g.floor.change("primary", "redirected", 100)
                self.at(101)
                g.observe("primary", turn("created", "Yeah. Get it onto my phone. Take as long as you need, just send",
                                          40000, 49800, tid="p3"))
                utterance_ends(g)
                self.assertEqual(g.floor.owner, "primary", "fixture: a follow-up request")
                self.at(101.1)
                g.observe("primary", {"type": "turn.delta", "turn_id": "p3", "delta": " receipts",
                                      "start_ms": 49800 + gap, "end_ms": 50000 + gap})
                utterance_ends(g)
                self.assertEqual(g.floor.owner, owner)
                await g.close()

    async def test_a_hand_off_line_is_muted_at_its_promise_until_the_answer(self):
        # 52.998 s: the reply to line 3 opened "Okay, I'll" and said "Okay, I'll take that".
        g = self.guest
        g.set_people(2)
        g.floor.change("primary", "redirected", 100)
        self.at(101)
        g.observe("primary", turn("done", "Can you get it onto my laptop?", 1000, 2000, tid="u3"))
        utterance_ends(g)
        await asyncio.sleep(0)
        [(did, _)] = self.call.delegated
        self.assertTrue(g.observe("primary", turn("created", " Okay,", role="assistant", tid="r1")))
        self.assertFalse(g.observe("primary", output(" I'll")))
        self.assertFalse(g.observe("primary", output(" take that.")))
        self.assertIn("floor.promise_muted", self.call.traces)
        self.speak(did, "The laptop is on build four.")
        self.assertTrue(g.observe("primary", output(" The laptop is on build four.")), "its answer is heard")

    def test_a_muted_line_never_reaches_the_phone_at_its_turn_done(self):
        # 55.151 s: unmuted before it ended, the reply's turn.done put "Okay, I'll take that" in the phone's transcript.
        protocol = live.Protocol(host_delegation=True)
        protocol.from_provider(output(" Okay,"), assistant_audible=True)
        protocol.from_provider(output(" I'll take that."), assistant_audible=False)
        protocol.from_provider(output(" The laptop is on build four."), assistant_audible=True)
        out = protocol.from_provider(turn("done", " Okay, I'll take that. The laptop is on build four.",
                                          role="assistant", tid="r1"), assistant_audible=True)
        self.assertNotIn("take that", json.dumps(out))


class OneDecisionTest(TimedGuestTest):
    async def test_with_people_on_the_call_the_floor_decides_once_per_utterance_from_the_primary_leg(self):
        # Take 9: one leg heard "Hey Tek, while" (floor to Tek mid-line); the other leg's final of the same utterance
        # read "Hey Tech, while that runs, ..." and flipped the floor to the people, so Tek never answered.
        g = self.guest
        g.set_people(2)
        g.observe("primary", turn("created", "Hey Tek, while", 64200, 65900, tid="p4"))
        self.assertEqual(g.floor.owner, "people", "no decision while the caller is still talking")
        g.observe("guest", turn("done", "Hey Teak, while that runs, what caused the crash in PR 28", 65000, 69900,
                                tid="g4"))
        g.observe("guest", {"type": "delegation.created", "item": {"id": "gd4", "target": "client"}})
        self.at(101)
        g.observe("primary", turn("done", "Hey Tek, while that runs, what caused the crash in PR 28", 64200, 69900,
                                  tid="p4"))
        self.assertEqual(g.floor.owner, "people", "nor at the provider's turn.done")
        utterance_ends(g)
        self.assertEqual((g.floor.owner, g.floor.reason), ("guest", "guest_addressed"))
        await asyncio.sleep(0)
        self.assertEqual([c.args[3] for c in self.tek.call_args_list], ["Tek, while that runs, what caused the crash "
                                                                         "in PR 28"], "asked once, with the routed words")
        self.at(120)
        g.observe("primary", turn("done", "Okay, nice. That's the one I kept hitting", 90000, 92500, tid="p5"))
        utterance_ends(g)
        self.assertEqual(g.floor.owner, "people")
        self.assertEqual(self.tek.call_count, 1)

    async def test_an_unnamed_follow_up_goes_to_the_bot_that_just_replied_and_waits_for_the_utterance_to_end(self):
        # Take 10: Hermes's "Right" 1.4 s into Kevin's long-run ask made "Yeah, merged isn't" his final; takes 10
        # and 11 never handed the ask to Hermes.
        g = self.guest
        g.set_people(2)
        g.observe("primary", turn("done", "Hermes, what's on my phone versus staging?", 1000, 3000, tid="p2"))
        utterance_ends(g)
        await asyncio.sleep(0)
        g.observe("primary", turn("created", "", role="assistant", tid="a2"))
        self.assertTrue(g.observe("primary", output("Your phone is on last night's build.")))
        self.at(110)
        g.observe("primary", turn("done", "", role="assistant", tid="a2"))
        self.at(111)
        g.observe("primary", turn("created", "Yeah, merged isn't", 11000, 12000, tid="p3"))
        g.observe("primary", turn("created", "", role="assistant", tid="a3"))
        self.assertFalse(g.observe("primary", output("Right.")), "the floor has not decided: the bot stays muted")
        g.observe("primary", turn("done", "Yeah, merged isn't", 11000, 12000, tid="p3"))
        self.call.input_loud_at = 113.9  # Kevin is still talking; his words trail the mic
        self.at(114)
        g.end_of_utterance()
        self.assertEqual(len(self.call.delegated), 1, "no decision while the mic is loud")
        g.observe("primary", {"type": "turn.delta", "turn_id": "p3", "delta": " done. Get all of it onto my S25",
                              "start_ms": 12500, "end_ms": 15000})
        self.at(115)  # mic quiet since 113.9
        utterance_ends(g)
        await asyncio.sleep(0)
        self.assertEqual(g.floor.owner, "primary")
        self.assertEqual(self.call.delegated[1][1], "Yeah, merged isn't done. Get all of it onto my S25")


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


# The chat a call opened from (take 7 of the 11-22 film call, shortened): the work is hermes-android.
FILM_CHAT = [{"role": "user", "content": [{"type": "input_text", "text": "status on getting hermes-android staging onto "
                                                                        "my phone?"}]},
             {"role": "assistant", "content": [{"type": "output_text", "text": "24 PRs merged into staging, including "
                                                "the Glass theme and a fix for the Glass crash on launch (PR 28). A "
                                                "long run is already going for this: card t_0a96e2de."}]}]


class GuestContextTest(unittest.IsolatedAsyncioTestCase):
    """Take 7: Jules asked Tek "what caused the glass crash in PR twenty eight?". Tek's Hermes got only those words,
    searched the wrong repo and said "I can't find any glass crash in PR 28"; the call's chat said hermes-android."""

    async def call(self):
        config = guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek")
        with patch.dict(live.os.environ, {"HERMES_VOICE_ENGINE": "gpt-live"}), \
                patch.object(live.guest_voice, "configured", return_value=config):
            call = live.Call(live.Manager(None), profile="")
        self.addAsyncCleanup(call.close, "test")
        call.relay, call.heard_user = None, True
        call.read_history(FILM_CHAT)
        return call

    async def test_tek_hermes_gets_the_calls_chat_context_with_the_request(self):
        call = await self.call()
        got = []

        async def host(http, profile, session, request, context=""):
            got.append((request, context))
            return "PR 28 fixed the crash."

        guest = call.guest
        guest.floor.available = True
        with patch.object(live, "ask_guest_host", host):
            call.message(json.dumps(turn("done", "Hey, Tek. What caused the glass crash in PR twenty eight?", 1000,
                                         5000, tid="p1")), True)
            guest.message(json.dumps(turn("created", "", 1000, 1200, tid="u1")))
            guest.message(json.dumps(turn("done", "Hey, Tek. What caused the glass crash in PR twenty eight?", 1000,
                                          5000, tid="u1")))
            guest.message(json.dumps({"type": "delegation.created", "item": {
                "id": "gd1", "target": "client", "content": [{"text": "what caused the glass crash in PR 28"}]}}))
            await asyncio.gather(*call.tasks)
        [(request, context)] = got
        self.assertIn("PR twenty eight", request)
        self.assertIn("hermes-android", context)
        self.assertIn("Glass crash on launch (PR 28)", context)

    async def test_the_guest_host_request_carries_the_context_as_background(self):
        received = []

        async def completion(request):
            received.append((await request.json())["messages"])
            return web.json_response({"choices": [{"message": {"content": "ok"}}]},
                                     headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]})

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
            with patch.object(Path, "home", return_value=Path(home)), patch.dict(
                    live.os.environ, {"Z0LIVE_GUEST_HOST_PORT": str(runner.addresses[0][1])}):
                async with ClientSession() as http:
                    await live.ask_guest_host(http, "tek", "s1", "What caused it?", "The work is hermes-android.")
        [messages] = received
        self.assertEqual(messages[-1], {"role": "user", "content": "What caused it?"})
        self.assertTrue(any(m["role"] == "system" and "The work is hermes-android." in m["content"] for m in messages))

    @patch.object(live, "HOST_DELEGATION", True)
    async def test_kevins_hermes_on_the_oneshot_path_gets_the_same_context(self):
        call = await self.call()
        call.protocol.seen.append("d1")
        call.send = lambda *a, **k: None
        asked = []
        with patch.object(live, "run_hermes", lambda request, profile, own_lane=False: asked.append(request) or "ok"), \
                patch.object(live, "TIERS_ON", True), patch.object(live.Call, "tier_lanes", AsyncMock()):
            await call.host_delegate("d1", "Get all of it onto my S25")
        [request] = asked
        self.assertIn("card t_0a96e2de", request)
        self.assertTrue(request.rstrip().endswith("Get all of it onto my S25"))


PR_28 = "kvnloo/hermes-android#28"
PR_28_TITLE = "fix(glass): stop the crash on every launch after picking Glass"
# The brief the chat agent wrote for the call (hermes-voice-brief): the primary voice opens with it.
FILM_BRIEF = (f"Glass on Kevin's S25: {PR_28} '{PR_28_TITLE}' is merged into hermes-android staging; the phone still "
              "runs last night's 4.0.9.")
# A chat whose words never name the repo: only the written brief places "PR 28".
VAGUE_CHAT = [{"role": "user", "content": [{"type": "input_text", "text": "is the glass fix on my phone yet?"}]}]
REPO_CHAT = [{"role": "user", "content": [{"type": "input_text", "text": "status on getting hermes-android staging onto "
                                                                        "my phone?"}]},
             {"role": "assistant", "content": [{"type": "output_text", "text": f"Merged into staging: {PR_28} "
                                                f"'{PR_28_TITLE}', and the Glass theme."}]}]
BOAT_CHAT = [{"role": "user", "content": [{"type": "input_text", "text": "when is the boat inspection?"}]}]


class GuestCallBriefTest(unittest.IsolatedAsyncioTestCase):
    """Tek's Hermes gets the call brief the primary voice opened with, not only the chat's raw lines (take 7: the
    words to Tek said "PR 28"; which repo that is lives in the brief and the chat the call opened from)."""

    async def call(self, history, brief=""):
        config = guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek")
        with patch.dict(live.os.environ, {"HERMES_VOICE_ENGINE": "gpt-live"}), \
                patch.object(live.guest_voice, "configured", return_value=config):
            call = live.Call(live.Manager(None), profile="")
        self.addAsyncCleanup(call.close, "test")
        call.relay, call.heard_user = None, True
        with patch.object(live.call_brief, "written", return_value=brief):  # the brief file store
            call.read_history(history)
        return call

    async def ask_tek(self, call, words="Hey, Tek. What caused the glass crash in PR twenty eight?"):
        got = []

        async def host(http, profile, session, request, context=""):
            got.append(SimpleNamespace(profile=profile, session=session, request=request, context=context))
            return "PR 28 fixed the crash."

        guest = call.guest
        guest.floor.available = True
        with patch.object(live, "ask_guest_host", host):
            call.message(json.dumps(turn("done", words, 1000, 5000, tid="p1")), True)
            guest.message(json.dumps(turn("created", "", 1000, 1200, tid="u1")))
            guest.message(json.dumps(turn("done", words, 1000, 5000, tid="u1")))
            guest.message(json.dumps({"type": "delegation.created", "item": {
                "id": "gd1", "target": "client", "content": [{"text": "what caused the glass crash in PR 28"}]}}))
            await asyncio.gather(*call.tasks)
        [asked] = got
        return asked

    async def test_tek_hermes_gets_the_written_call_brief_the_primary_opened_with(self):
        call = await self.call(VAGUE_CHAT, FILM_BRIEF)
        asked = await self.ask_tek(call)
        self.assertIn(PR_28, asked.context)
        self.assertIn(PR_28_TITLE, asked.context)
        self.assertIn("is the glass fix on my phone yet?", asked.context)  # the chat's lines still go with it
        self.assertEqual((asked.profile, asked.session), ("tek", "z0live-guest-" + call.id))  # still Tek's own Hermes

    async def test_a_call_opened_with_only_a_written_brief_still_gives_tek_hermes_context(self):
        call = await self.call([], FILM_BRIEF)
        asked = await self.ask_tek(call)
        self.assertIn(PR_28, asked.context)

    async def test_the_brief_reaches_tek_hermes_without_the_voice_only_framing(self):
        call = await self.call(VAGUE_CHAT, FILM_BRIEF)
        asked = await self.ask_tek(call)
        self.assertIn(PR_28, asked.context)
        self.assertNotIn("<call_brief>", asked.context)  # framing for the voice model, not for a Hermes run
        self.assertNotIn("wait for Kevin to speak", asked.context)

    async def test_each_call_gives_tek_hermes_only_its_own_context(self):
        film = await self.call(REPO_CHAT, FILM_BRIEF)
        boat = await self.call(BOAT_CHAT, "Boat inspection is Tuesday at 9.")
        boat_asked = await self.ask_tek(boat, "Hey, Tek. When is the boat inspection?")
        film_asked = await self.ask_tek(film)
        self.assertIn("Boat inspection is Tuesday", boat_asked.context)
        self.assertNotIn("hermes-android", boat_asked.context)
        self.assertNotIn("Glass", boat_asked.context)
        self.assertIn(PR_28_TITLE, film_asked.context)
        self.assertNotIn("boat", film_asked.context.lower())
        self.assertNotEqual(film_asked.session, boat_asked.session)

    async def test_tek_hermes_request_payload_places_pr_28_in_hermes_android(self):
        """The wire payload Tek's Hermes receives, through the real ask_guest_host, from a call whose history names
        kvnloo/hermes-android#28."""
        received = []

        async def completion(request):
            received.append((dict(request.headers), (await request.json())["messages"]))
            return web.json_response({"choices": [{"message": {"content": "A null theme on first launch."}}]},
                                     headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        call = await self.call(REPO_CHAT)
        guest = call.guest
        guest.floor.available = True
        with tempfile.TemporaryDirectory() as home:
            keys = Path(home) / ".config/z0live/guest-hosts"
            keys.mkdir(parents=True)
            (keys / "tek.key").write_text("tek-key")
            with patch.object(Path, "home", return_value=Path(home)), patch.dict(
                    live.os.environ, {"Z0LIVE_GUEST_HOST_PORT": str(runner.addresses[0][1])}):
                async with ClientSession() as http:
                    call.manager.http = http
                    call.message(json.dumps(turn("done", "Hey, Tek. What caused the glass crash in PR twenty eight?",
                                                 1000, 5000, tid="p1")), True)
                    guest.message(json.dumps(turn("created", "", 1000, 1200, tid="u1")))
                    guest.message(json.dumps(turn("done", "Hey, Tek. What caused the glass crash in PR twenty "
                                                  "eight?", 1000, 5000, tid="u1")))
                    guest.message(json.dumps({"type": "delegation.created", "item": {
                        "id": "gd1", "target": "client",
                        "content": [{"text": "what caused the glass crash in PR 28"}]}}))
                    await asyncio.gather(*call.tasks)
        [(headers, messages)] = received
        self.assertEqual(headers["Authorization"], "Bearer tek-key")
        self.assertEqual(headers["X-Hermes-Session-Id"], "z0live-guest-" + call.id)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("answering as a guest", messages[0]["content"])
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("PR twenty eight", messages[-1]["content"])
        background = "\n".join(m["content"] for m in messages[1:-1] if m["role"] == "system")
        self.assertIn(PR_28, background)
        self.assertIn(PR_28_TITLE, background)


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

        # Tek's leg hears the caller's earlier question to Hermes in the same turn as the address to Tek. The primary
        # stays silent while Tek talks, so its leg keeps ONE user turn open across both exchanges.
        primary(turn("created", "", 1000, 1200, tid="p1"))
        guest(turn("created", "", 1000, 1200, tid="g1"))
        primary(delta("Tek, what did you work on today?", 3500, 6000))
        guest(turn("done", "Hey, can you check the garden project? Tek, what did you work on today?", 1000, 6000,
                   tid="g1"))
        guest({"type": "delegation.created", "item": {"id": "gd1", "target": "client", "content": [
            {"type": "input_text", "text": "what did you work on today"}]}})
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
