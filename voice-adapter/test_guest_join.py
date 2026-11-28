"""A second agent joins a live call by voice ("add Tek to the call"), for that call only, in its own voice."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import guest_join
import guest_voice
import mobile_live as live
from test_gpt_guest import PrimaryCall, output, turn


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"Z0LIVE_JOIN_HOSTS": json.dumps({"tek": 18644, "pc": 18645})}))

    def test_join_commands_kevin_would_say(self):
        for text, expected in [
            ("Add Tek to the call", ("add", "tek")),
            ("add tech to this call please", ("add", "tek")),  # the transcriber's "tech"
            ("Hey Hermes, add the tek bot to the call", ("add", "tek")),
            ("add teknium to the call", ("add", "tek")),
            ("Can you bring PC into the call?", ("add", "pc")),
            ("add P.C. to the call", ("add", "pc")),
            ("bring in tek", ("add", "tek")), ("Bring Tek in", ("add", "tek")), ("let's add Tek", ("add", "tek")),
            ("get Tek on the call", ("add", "tek")), ("I want PC on this call", ("add", "pc")),
            ("Remove Tek from the call", ("remove", "tek")), ("drop tek", ("remove", "tek")),
            ("Tek, you can go", ("remove", "tek")), ("kick PC off the call", ("remove", "pc")),
        ]:
            with self.subTest(text=text):
                self.assertEqual(guest_join.parse(text), expected)

    def test_other_words_are_not_join_commands(self):
        for text in ("add the tek repo to the list", "tell Tek to add a card", "Tek, what did you work on today?",
                     "add five haiku lanes", "ask @pc to list the drive", "add guest to the call",
                     "add default to the call", "add Julia to the call", "Tek worked on the call notes"):
            with self.subTest(text=text):
                self.assertIsNone(guest_join.parse(text))

    def test_each_guest_has_its_own_voice_never_the_primarys(self):
        self.assertEqual(guest_join.config("tek"), guest_voice.GuestConfig("gpt-live", "Tek", "ember", "tek"))
        self.assertEqual(guest_join.config("pc"), guest_voice.GuestConfig("gpt-live", "PC", "cove", "pc"))
        self.assertIsNone(guest_join.config("guest"))
        self.assertIsNone(guest_join.config("nobody"))
        for name in ("z0", "poteto", "matt", "kun"):
            self.assertIn(guest_join.voice(name), guest_join.VOICES)
            self.assertNotEqual(guest_join.voice(name), guest_join.PRIMARY_VOICE)
            self.assertNotIn(guest_join.voice(name), ("ember", "cove"))

    def test_hosts_default_to_tek_and_the_ask_hosts(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            os.environ.pop("Z0LIVE_JOIN_HOSTS", None)
            with patch.dict(os.environ, {"Z0LIVE_GUEST_HOST_PORT": "19000"}):
                self.assertEqual(guest_join.hosts(), {"tek": 19000, "pc": 18645})


class JoinCall(PrimaryCall):
    """The primary call with the real join path (Call methods) and a fake transport."""
    engine, leg, profile, people, closed = "gpt-live", None, "", 1, False
    microphone = None
    join_words = live.Call.join_words
    join_command = live.Call.join_command
    join = live.Call.join
    add_guest = live.Call.add_guest
    remove_guest = live.Call.remove_guest
    speak_for = live.Call.speak_for

    def __init__(self):
        super().__init__()
        self.guest = self.duplex = None
        self.counts = live.Counter()
        self.protocol.turn_text = {"user": "", "assistant": ""}
        self.events = []

    def trace(self, ev, **kw):
        self.traces.append(ev)
        self.events.append((ev, kw))

    async def renew_subscription(self, guest=None):
        return None


class JoinTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = self.enterContext(tempfile.TemporaryDirectory())
        keys = Path(self.home) / ".config/z0live/guest-hosts"
        keys.mkdir(parents=True)
        (keys / "tek.key").write_text("tek-key")
        self.voices = Path(self.home) / "voices.json"
        self.voices.write_text("{}")
        self.enterContext(patch.object(Path, "home", return_value=Path(self.home)))
        self.enterContext(patch.dict(os.environ, {"HOME": self.home, "Z0LIVE_VOICES_FILE": str(self.voices),
                                                  "Z0LIVE_JOIN_HOSTS": json.dumps({"tek": 18644, "pc": 18645})}))
        self.up = AsyncMock(return_value=True)
        self.call = JoinCall()
        self.call.guest_host_up = self.up

        async def start(guest):  # the guest's GPT-Live session comes up
            guest.message(json.dumps({"type": "session.started"}))
        self.enterContext(patch.object(live.GPTGuest, "start", start))
        self.enterContext(patch.object(live.GPTGuest, "send", lambda g, event: self.call.sent.append(("guest", event))))

    async def asyncTearDown(self):
        for voice in {self.call.guest, self.call.duplex} - {None}:
            await voice.close()

    def spoken(self, leg="primary"):
        """Texts handed to a voice to speak, per leg."""
        out = []
        for msg in self.call.sent:
            who, event = msg if isinstance(msg, tuple) else ("primary", msg)
            if who == leg and isinstance(event, dict) and event.get("channel") == "speakable":
                text = "".join(c.get("text", "") for c in event.get("content", []))
                out.append(text.split("\n\n", 1)[-1] if text.startswith('"Agent Final Message"') else text)
        return out

    def primary_says(self, text, tid):
        """Hermes's voice speaks a reply (what the phone hears, from the provider's transcript)."""
        g = self.call.duplex
        g.observe("primary", turn("created", "", role="assistant", tid=tid))
        g.observe("primary", output(text))
        g.observe("primary", turn("done", text, role="assistant", tid=tid))

    async def settle(self, until, limit=3.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + limit
        while not until() and loop.time() < end:
            await asyncio.sleep(0.02)

    async def test_add_tek_turns_the_guest_on_for_this_call_only_and_tek_says_hello_in_its_own_voice(self):
        self.call.join(("add", "tek"), None, "Add Tek to the call")
        await self.settle(lambda: self.spoken())
        self.assertEqual(self.spoken(), ["Bringing Tek in."])
        g = self.call.duplex
        self.assertIsInstance(g, live.GPTGuest)
        self.assertIs(self.call.guest, g)
        self.assertEqual((g.config.profile, g.config.voice, g.floor.available), ("tek", "ember", True))
        self.primary_says("Bringing Tek in.", "r1")
        await asyncio.gather(*self.call.tasks)
        self.assertEqual(len(self.spoken("guest")), 1)
        self.assertIn("Hi Kevin, Tek here.", self.spoken("guest")[0])
        self.assertEqual((g.floor.owner, g.floor.reason), ("guest", "guest_joined"))
        self.assertIn(g.config.primary_note, self.call.sent)  # Hermes is told a second voice is on
        # Never written: the next call starts with one voice again.
        self.assertEqual(self.voices.read_text(), "{}")
        self.assertIsNone(guest_voice.configured())
        # Kevin's next words to Tek go to Tek; "Hermes, ..." comes back to Hermes.
        self.assertIs(g.addressing("Tek, what shipped?", final=True), live.Address.GUEST)
        self.assertIs(g.addressing("Hermes, back to you.", final=True), live.Address.OTHER)

    async def test_remove_tek_leaves_hermes_alone_on_the_call(self):
        self.call.join(("add", "tek"), None)
        await self.settle(lambda: self.spoken())
        self.primary_says("Bringing Tek in.", "r1")
        await asyncio.gather(*self.call.tasks)
        g = self.call.duplex
        self.call.join(("remove", "tek"), None)
        await asyncio.gather(*self.call.tasks)
        self.assertTrue(g.closed)
        self.assertIsNone(self.call.guest)
        self.assertEqual((g.floor.owner, g.floor.reason, g.floor.available), ("primary", "guest_left", False))
        self.assertIn("Tek is off the call now.", self.spoken())
        self.assertFalse(any("disconnected" in text for text in self.spoken()))
        # Tek's hello, unsaid when it left, is not handed to Hermes as "Tek's Hermes returned this answer".
        self.assertFalse(any("joined Kevin's call" in text for text in self.spoken()))

    async def test_turn_and_delegation_of_one_utterance_add_once(self):
        self.call.protocol.turn_text["user"] = "Hey, add Tek to the call."
        self.call.join(self.call.join_words("Hey, add Tek to the call."), None, "Hey, add Tek to the call.")
        # The voice then delegates its paraphrase of the same words.
        self.assertTrue(self.call.join_command("d1", "Kevin wants Tek added to this call"))
        await self.settle(lambda: self.spoken())
        self.primary_says("Bringing Tek in.", "r1")
        await asyncio.gather(*self.call.tasks)
        self.assertEqual(self.call.counts["guest_join_add"], 1)
        self.assertIn("guest.join_repeat", self.call.traces)
        self.assertEqual(self.spoken(), ["Bringing Tek in."])

    async def test_a_request_that_does_not_name_a_joinable_agent_is_not_swallowed(self):
        self.call.protocol.turn_text["user"] = "Add Tek to the call"
        self.assertFalse(self.call.join_command("d2", "what's the weather"))

    async def test_an_unreachable_host_is_said_and_nothing_joins(self):
        self.up.return_value = False
        self.call.join(("add", "tek"), None)
        await asyncio.gather(*self.call.tasks)
        self.assertIsNone(self.call.duplex)
        self.assertEqual(self.spoken(), ["I can't reach Tek's Hermes, so Tek can't join right now."])

    async def test_a_profile_without_a_voice_host_cannot_join(self):
        self.call.join(("add", "pc"), None)  # pc has a port but no guest-hosts/pc.key here
        await asyncio.gather(*self.call.tasks)
        self.assertIsNone(self.call.duplex)
        self.assertIn("guest.join_refused", self.call.traces)
        self.up.assert_not_awaited()

    async def test_one_guest_at_a_time(self):
        (Path(self.home) / ".config/z0live/guest-hosts/pc.key").write_text("pc-key")
        self.call.join(("add", "tek"), None)
        await self.settle(lambda: self.spoken())
        self.primary_says("Bringing Tek in.", "r1")
        await asyncio.gather(*self.call.tasks)
        self.call.join(("add", "pc"), None)
        await asyncio.gather(*self.call.tasks)
        self.assertEqual(self.call.guest.config.profile, "tek")
        # Tek holds the floor after its hello, so Hermes's line waits for it (Kevin's next words move it back).
        waiting = [a.text for a in self.call.duplex.floor.answers["primary"]]
        self.assertTrue(any("say remove Tek first" in text for text in self.spoken() + waiting))

    async def test_people_on_the_call_keep_their_floor_when_tek_joins(self):
        floor_only = live.GPTGuest(self.call, None)  # Kevin and a friend at the phone, Hermes the only bot
        self.call.duplex, self.call.people = floor_only, 2
        floor_only.set_people(2)
        self.call.join(("add", "tek"), None)
        await self.settle(lambda: floor_only.config is not None and floor_only.floor.available)
        self.assertIs(self.call.duplex, floor_only)
        self.assertEqual(floor_only.floor.people, 2)
        self.primary_says("Bringing Tek in.", "r1")
        await asyncio.gather(*self.call.tasks)
        self.assertEqual(floor_only.config.profile, "tek")

    async def test_a_failed_join_says_so_and_never_says_disconnected(self):
        async def broken(guest):
            raise TimeoutError
        with patch.object(live.GPTGuest, "start", broken):
            self.call.join(("add", "tek"), None)
            await asyncio.gather(*self.call.tasks)
            await asyncio.sleep(0)
            await asyncio.gather(*self.call.tasks)
        self.assertIsNone(self.call.guest)
        self.assertIn("I couldn't bring Tek onto the call.", self.spoken())
        self.assertFalse(any("disconnected" in text for text in self.spoken()))


if __name__ == "__main__":
    unittest.main()
