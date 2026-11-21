"""Exercise the deployer, proxy, and two real adapter peers without launchd or a hosted model."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

import dashboard_proxy
import mobile_live as live
from rolling_restart import roll
from voice_slots import Route, Slots

PREFIX = "/api/audio/voice-live"
AUTH = {"Authorization": "Bearer synthetic-probe"}


class InProcessJobs:
    def __init__(self, loop, sockets):
        self.loop = loop
        self.sockets = sockets
        self.apps = {port: live.make_app() for port in sockets}
        self.stops = {port: asyncio.Event() for port in sockets}
        self.tasks = {}

    def pid(self, port):
        task = self.tasks.get(port)
        return os.getpid() if task and not task.done() else None

    def start(self, port):
        self.tasks[port] = asyncio.run_coroutine_threadsafe(live.serve(
            port, self.stops[port], app=self.apps[port], sock=self.sockets[port]), self.loop)

    def stop(self, port):
        self.loop.call_soon_threadsafe(self.stops[port].set)


async def until(predicate):
    while not predicate():
        await asyncio.sleep(0.02)


class RollingRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_call_keeps_media_while_new_call_uses_new_slot(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "HERMES_VOICE_ENGINE": "gpt-live", "Z0LIVE_GUEST": "off",
            "HERMES_VOICE_HANDOFF": "off", "HERMES_MOBILE_VOICE_TRACE": "0",
        }))
        self.enterContext(patch.multiple(live, CHAT_CONTINUITY=False, HOST_DELEGATION=False, DRAIN_SECONDS=10))
        self.enterContext(patch.object(live.call_brief, "build", return_value="Synthetic local probe"))
        self.enterContext(patch.object(live, "codex_headers", return_value={"Authorization": "Bearer fixture"}))
        config = RTCConfiguration(iceServers=[])

        def peer():
            pc = RTCPeerConnection(config)
            self.addAsyncCleanup(pc.close)
            return pc

        async def auth(request):
            self.assertEqual(request.headers.get("Authorization"), AUTH["Authorization"])
            return web.json_response({"user_id": "synthetic-probe"})

        async def signal(request):
            offer = await request.json()
            pc = peer()
            pc.addTrack(AudioStreamTrack())

            @pc.on("datachannel")
            def connected(channel):
                channel.send(json.dumps({"type": "session.started", "session": {"id": "local-fixture"}}))

            await pc.setRemoteDescription(RTCSessionDescription(offer["sdp"], "offer"))
            await pc.setLocalDescription(await pc.createAnswer())
            return web.Response(status=201, text=pc.localDescription.sdp, content_type="application/sdp")

        fixture = web.Application()
        fixture.router.add_get("/api/auth/me", auth)
        fixture.router.add_post("/signal", signal)
        fake = TestServer(fixture)
        await fake.start_server()
        self.addAsyncCleanup(fake.close)
        self.enterContext(patch.multiple(live, UPSTREAM=str(fake.make_url("")), SIGNALING=str(fake.make_url("/signal"))))

        sockets = {}
        for _ in range(2):
            sock = self.enterContext(socket.socket())
            sock.bind(("127.0.0.1", 0))
            sockets[sock.getsockname()[1]] = sock
        old, new = sockets
        slots = Slots(directory, (old, new))
        slots.write(Route(old))
        jobs = InProcessJobs(asyncio.get_running_loop(), sockets)
        old_request_entered = asyncio.Event()
        old_request_release = asyncio.Event()

        @web.middleware
        async def hold_old_diagnostics(request, handler):
            if request.path == f"{PREFIX}/diagnostics":
                old_request_entered.set()
                await old_request_release.wait()
            return await handler(request)

        jobs.apps[old].middlewares.append(hold_old_diagnostics)
        jobs.start(old)

        async def stop_jobs():
            for port in jobs.tasks:
                jobs.stop(port)
            await asyncio.gather(*(asyncio.wrap_future(task) for task in jobs.tasks.values()))

        self.addAsyncCleanup(stop_jobs)
        proxy = TestServer(dashboard_proxy.make_app(slots))
        await proxy.start_server()
        self.addAsyncCleanup(proxy.close)
        http = await self.enterAsyncContext(ClientSession())

        async def probe(port):
            pc = peer()
            pc.addTrack(AudioStreamTrack())
            channel = pc.createDataChannel("oai-events")
            tracks = asyncio.Queue()
            pc.on("track", tracks.put_nowait)
            await pc.setLocalDescription(await pc.createOffer())
            async with http.post(proxy.make_url(f"{PREFIX}/session"), headers=AUTH,
                                 json={"sdp": pc.localDescription.sdp}) as response:
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual(response.headers["X-Z0Live-Voice-Port"], str(port))
                result = await response.json()
            await pc.setRemoteDescription(RTCSessionDescription(result["transport"]["sdp"], "answer"))
            await until(lambda: channel.readyState == "open")
            return pc, channel, await tracks.get(), result["session"]["id"]

        release = directory / "new-release"
        python = release / "voice-adapter/.venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (release / "voice-adapter/mobile_live.py").touch()
        (release / "desktop").mkdir()

        async with asyncio.timeout(15):
            await until(lambda: "manager" in jobs.apps[old])
            first, first_dc, first_audio, first_id = await probe(old)
            await first_audio.recv()
            pending = asyncio.ensure_future(http.get(proxy.make_url(f"{PREFIX}/diagnostics"), headers=AUTH))
            rollout = None
            try:
                await old_request_entered.wait()
                assert isinstance(proxy.port, int)
                rollout = asyncio.create_task(asyncio.to_thread(roll, slots, jobs, release, proxy.port))
                await until(lambda: slots.read().active == new or rollout.done())
                if rollout.done():
                    await rollout
                second, second_dc, second_audio, second_id = await probe(new)
                self.assertFalse(jobs.stops[old].is_set())
                old_request_release.set()
                async with await pending as response:
                    self.assertEqual([call["id"] for call in (await response.json())["active"]], [first_id])
                await until(lambda: jobs.apps[old]["manager"].draining)
                counts = jobs.apps[old]["manager"].calls[first_id].counts
                received = counts["input_frames"]
                await until(lambda: counts["input_frames"] > received)
                await first_audio.recv()
                await second_audio.recv()
                self.assertEqual(first.connectionState, "connected")
                self.assertFalse(jobs.tasks[old].done())
                async with http.get(proxy.make_url(f"{PREFIX}/diagnostics"), headers=AUTH) as response:
                    self.assertEqual([call["id"] for call in (await response.json())["active"]], [second_id])
                first_dc.send(json.dumps({"type": "session.close"}))
                await rollout
                self.assertEqual(jobs.apps[old]["manager"].recent[-1]["reason"], "client_closed")
                self.assertTrue(jobs.tasks[old].done())
                self.assertEqual(second.connectionState, "connected")
                await second_audio.recv()
                second_dc.send(json.dumps({"type": "session.close"}))
                await until(lambda: not jobs.apps[new]["manager"].calls)
            finally:
                old_request_release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if rollout is not None and not rollout.done():
                    await first.close()
                    await rollout
