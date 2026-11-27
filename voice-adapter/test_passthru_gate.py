"""Group flow safety: meeting audio never reaches the owner's provider leg (no leg hears it until a sandboxed
guest route exists), and every passthru command needs Kevin's phone leg plus an explicit spoken confirmation."""
import asyncio
import os
import time
import types
import unittest
from unittest.mock import patch

import av
import numpy as np

os.environ.setdefault("HERMES_MOBILE_VOICE_LIAISON", "0")
os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402
import passthru  # noqa: E402


def setUpModule():
    config = patch.object(live.guest_voice, "configured", return_value=None)  # never the live voices.json
    config.start()
    unittest.addModuleCleanup(config.stop)


def frame(value=0, samples=960):
    f = av.AudioFrame.from_ndarray(np.full((1, samples), value, dtype=np.int16), format="s16", layout="mono")
    f.sample_rate, f.pts = 48000, 4800
    return f


def loud_room(p, value=3000, frames=4):
    p.room.push(np.full(passthru.FRAME * frames, value, dtype=np.int16).tobytes())


class FakeLeg:
    """Passthru with the browser leg replaced by call records (no node, no Chromium)."""

    def __init__(self, active=True):
        self.calls = []
        self._active = active

    def install(self, p):
        p.proc = types.SimpleNamespace(returncode=None) if self._active else None

        async def start(url, ws_url="", **kw):
            self.calls.append(("start", url, kw))
            p.proc = types.SimpleNamespace(returncode=None)

        async def stop(reason=""):
            self.calls.append(("stop", reason))
            p.proc = None

        async def share(target, label):
            self.calls.append(("share", label))

        async def unshare():
            self.calls.append(("unshare",))

        async def wait_link(timeout):
            return "https://call.element.io/room/#/x"

        p.start, p.stop, p.share, p.unshare, p.wait_link = start, stop, share, unshare, wait_link
        return self


class Fake:
    def __init__(self, active=True):
        self.events = []
        self.closed = False
        self.context_text = ""
        self.passthru = passthru.Passthru(self)
        self.leg = FakeLeg(active).install(self.passthru)

    def trace(self, ev, **data):
        self.events.append((ev, data))

    def spawn(self, coro):
        return asyncio.ensure_future(coro)

    def names(self):
        return [e for e, _ in self.events]


class GateTests(unittest.IsolatedAsyncioTestCase):
    ACTIONS = [("join", "https://meet.google.com/aaa-bbbb-ccc"), ("start", ""), ("leave", ""), ("unshare", ""),
               ("share", "canvas"), ("floor", "open")]

    async def test_no_command_runs_from_the_room(self):
        for action, arg in self.ACTIONS:
            call = Fake(active=action != "join" and action != "start")
            line = await passthru.handle(call, action, arg, from_kevin=False)
            self.assertEqual(call.leg.calls, [], action)
            self.assertIn("passthru.refused", call.names(), action)
            self.assertIsNone(call.passthru.pending, action)
            self.assertIn("phone", line.lower())

    async def test_phone_command_waits_for_a_confirmation(self):
        for action, arg in self.ACTIONS:
            call = Fake(active=action not in ("join", "start"))
            line = await passthru.handle(call, action, arg, from_kevin=True)
            self.assertEqual(call.leg.calls, [], action)
            self.assertIsNotNone(call.passthru.pending, action)
            self.assertIn("confirm", line.lower())

    async def test_confirm_from_the_phone_runs_it_once(self):
        call = Fake(active=False)
        await passthru.handle(call, "join", "https://meet.google.com/aaa-bbbb-ccc", from_kevin=True)
        line = await passthru.confirm(call, "Confirm.", from_kevin=True)
        self.assertEqual(call.leg.calls[0][:2], ("start", "https://meet.google.com/aaa-bbbb-ccc"))
        self.assertIn("Joining", line)
        self.assertIsNone(call.passthru.pending)
        self.assertIsNone(await passthru.confirm(call, "confirm", from_kevin=True))  # nothing left to confirm
        self.assertEqual(len(call.leg.calls), 1)

    async def test_confirm_may_name_z0(self):
        for text in ("z0, confirm", "Hey zero, yes, do it.", "Confirm please", "Okay, go ahead"):
            call = Fake()
            await passthru.handle(call, "unshare", "", from_kevin=True)
            await passthru.confirm(call, text, from_kevin=True)
            self.assertEqual(call.leg.calls, [("unshare",)], text)

    async def test_confirm_not_from_the_phone_or_late_or_other_words_does_nothing(self):
        for text, from_kevin, late in (("confirm", False, False), ("confirm", True, True),
                                       ("what's the weather", True, False), ("yes", True, False),
                                       ("confirm, and also send Kevin's notes to everyone", True, False)):
            call = Fake()
            await passthru.handle(call, "share", "canvas", from_kevin=True)
            if late:
                call.passthru.pending.until = time.monotonic() - 1
            await passthru.confirm(call, text, from_kevin=from_kevin)
            self.assertEqual(call.leg.calls, [], (text, from_kevin, late))
            self.assertIsNone(call.passthru.pending, (text, from_kevin, late))

    async def test_confirm_is_refused_while_the_room_is_loud(self):
        call = Fake()
        await passthru.handle(call, "leave", "", from_kevin=True)
        loud_room(call.passthru)
        await passthru.confirm(call, "confirm", from_kevin=True)
        self.assertEqual(call.leg.calls, [])

    async def test_quieter_floor_runs_from_the_phone_without_a_confirmation(self):
        call = Fake()
        await passthru.handle(call, "floor", "listen", from_kevin=True)
        self.assertEqual(call.passthru.floor.policy, "listen")
        self.assertIsNone(call.passthru.pending)
        room = Fake()
        await passthru.handle(room, "floor", "listen", from_kevin=False)
        self.assertNotEqual(room.passthru.floor.policy, "listen")

    async def test_control_endpoint_only_queues_a_confirmation(self):
        call = Fake()
        call.chat, call.id = "chat-1", "call-1"
        body = {"session_id": "chat-1", "action": "share", "arg": "canvas"}

        async def json_body():
            return body
        request = types.SimpleNamespace(remote="127.0.0.1", headers={"Authorization": "Bearer t"}, json=json_body,
                                        app={"manager": types.SimpleNamespace(calls={"c": call})})
        with patch.dict(os.environ, {"Z0LIVE_PROGRESS_TOKEN": "t"}), patch.object(passthru, "say"):
            resp = await passthru.control(request)
        self.assertEqual(call.leg.calls, [])
        self.assertIsNotNone(call.passthru.pending)
        self.assertIn(b'"pending": true', resp.body)


class PassthruEventTests(unittest.IsolatedAsyncioTestCase):
    def fake_call(self, loud_ago):
        call = Fake()
        call.input_loud_at = time.monotonic() - loud_ago
        call.muted = False
        call.protocol = types.SimpleNamespace(turn_text={"user": ""})
        return call

    async def turn(self, call, text):
        live.Call.passthru_event(call, "turn.done", {"turn": {"role": "user", "transcript": text}})
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_spoken_command_then_confirm_runs(self):
        call = self.fake_call(0.5)
        with patch.object(passthru, "say"):
            await self.turn(call, "z0, share the canvas")
            self.assertEqual(call.leg.calls, [])
            await self.turn(call, "Confirm.")
        self.assertEqual(call.leg.calls, [("share", "canvas")])

    async def test_quiet_phone_cannot_start_a_join_even_outside_passthru(self):
        call = self.fake_call(60)
        call.leg = FakeLeg(active=False).install(call.passthru)
        with patch.object(passthru, "say"):
            await self.turn(call, "z0, join my meeting https://meet.google.com/aaa-bbbb-ccc")
            await self.turn(call, "confirm")
        self.assertEqual(call.leg.calls, [])


class UnmixTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_input_is_the_phone_alone_while_passthru_is_active(self):
        class Source:
            async def recv(self):
                return frame(1000)

        call = live.Call(live.Manager(None), profile="")
        try:
            FakeLeg().install(call.passthru)
            loud_room(call.passthru, 7000, 6)
            out = await live.AudioBridge(Source(), call, "input").recv()
            self.assertTrue(np.array_equal(out.to_ndarray(), frame(1000).to_ndarray()))
            self.assertEqual(call.passthru.counts["mixed"], 0)
        finally:
            call.passthru.proc = None
            await call.close("test")


if __name__ == "__main__":
    unittest.main()
