"""A named GPT-Live guest must own its floor and its Hermes session."""
import asyncio
from array import array
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web

import guest_voice
import mobile_live as live
from test_guest_voice import FakeCall, Source, frame


def delta(text):
    return {"type": "input_transcript.added", "item": {"text": text}}


class PrimaryCall(FakeCall):
    id = "test-call"
    profile = "poteto"
    muted = False
    manager: SimpleNamespace

    def __init__(self):
        super().__init__()
        self.tasks = []

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

    async def test_only_the_current_turn_owns_the_phone_output(self):
        primary = frame(value=7)
        switch = guest_voice.FloorSwitch(Source(primary), self.guest)
        self.guest.begin_turn()
        self.guest.observe(delta("Hey, can you hear me?"), "")
        self.assertIs(await switch.recv(), primary)

        self.guest.begin_turn()
        self.guest.observe(delta("Teknium, what shipped?"), "")
        self.guest.audio.extend(array("h", [1000] * 960).tobytes())
        self.assertTrue((await switch.recv()).to_ndarray().min() == 1000)
        self.assertFalse((await switch.recv()).to_ndarray().any())

        self.guest.begin_turn()
        self.guest.observe(delta(" Thanks. What's twelve times twelve?"), "")
        self.guest.observe({"type": "turn.done", "turn": {"role": "user", "transcript":
                           "Teknium, what shipped? Thanks. What's twelve times twelve?"}}, "")
        self.assertIs(await switch.recv(), primary)

    async def test_spoken_note_survives_a_late_provider_final(self):
        self.guest.begin_turn()
        self.guest.message(json.dumps(delta("Tek, what shipped?")))
        turn = {"id": "spoken", "role": "assistant"}
        self.guest.message(json.dumps({"type": "turn.created", "turn": turn}))
        self.guest.begin_turn()
        self.guest.message(json.dumps({"type": "turn.done", "turn": {**turn, "transcript": "A release."}}))
        self.assertEqual(len(self.call.sent), 1)
        self.assertIn("A release.", self.call.sent[0])
        self.assertFalse(self.guest.floor())

    async def test_only_addressed_delegation_reaches_the_guest_host(self):
        received = []

        async def completion(request):
            received.append((request.headers.get("Authorization"), request.headers.get("X-Hermes-Session-Id"),
                             (await request.json())["messages"][-1]["content"]))
            return web.json_response({"choices": [{"message": {"content": "A release."}}]},
                                     headers={"X-Hermes-Session-Id": "z0live-guest-test-call"})

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
                live.os.environ, {"Z0LIVE_GUEST_HOST_PORT": str(runner.addresses[0][1])}
            ):
                async with ClientSession() as http:
                    self.call.manager = SimpleNamespace(http=http)
                    self.guest.send = lambda event: self.call.sent.append(event)
                    for did, text in (("unaddressed", "What is this?"), ("addressed", "Tek, what shipped?")):
                        self.guest.begin_turn()
                        self.guest.message(json.dumps(delta(text)))
                        self.guest.message(json.dumps({"type": "delegation.created", "item": {
                            "id": did, "target": "client", "content": [{"text": text}]}}))
                        await asyncio.gather(*self.call.tasks)
        self.assertEqual(received, [("Bearer tek-key", "z0live-guest-test-call", "Tek, what shipped?")])
        [answer] = self.call.sent
        self.assertEqual(answer["delegation_item_id"], "addressed")
        self.assertIn("A release.", answer["content"][0]["text"])

    def test_config_reloads_and_keeps_legacy_grok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voices.json"
            with patch.dict(live.os.environ, {"Z0LIVE_VOICES_FILE": str(path), "Z0LIVE_GUEST": ""}):
                path.write_text('{"guest":"grok"}')
                config = guest_voice.configured()
                assert config is not None
                self.assertEqual(config.engine, "grok")
                path.write_text('{"guest":{"engine":"gpt-live","profile":"tek","name":"tek","voice":"ember"}}')
                self.assertEqual(guest_voice.configured(), self.guest.config)
                path.write_text('{"guest":{"engine":"gpt-live","profile":"../poteto","name":"tek","voice":"ember"}}')
                self.assertIsNone(guest_voice.configured())
