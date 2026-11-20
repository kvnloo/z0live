"""Deterministic protocol/auth tests and real localhost WebRTC fixture tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

# Unit tests never spawn a real lane or driver: tests patch live.run_hermes and use fake relays.
os.environ.setdefault('HERMES_MOBILE_VOICE_LIAISON', '0')
from unittest.mock import patch

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config (~/.z0int)
import mobile_live as live  # noqa: E402


class ChunkTests(unittest.TestCase):
    def test_dense_brief_never_exceeds_the_500_token_append_limit(self):
        # 11-20: a 1800-byte brief chunk of IDs/paths/numbers went over 500 tokens and the endpoint dropped it.
        dense = "\n".join(f"- commit 3d4363a{i:04d} ~/.local/share/z0live/bench/x{i}.json p=0.{i:03d} ✓ é" for i in range(300))
        parts = live.chunks(dense, 1800)
        self.assertEqual("".join(parts), dense)  # lossless
        self.assertTrue(all(len(p.encode("utf-8")) <= live.MAX_APPEND_BYTES for p in parts))
        self.assertTrue(all(p.endswith(("\n", " ")) for p in parts[:-1]))  # breaks on boundaries when possible

    def test_unbroken_text_still_splits_losslessly(self):
        s = "x" * 1234 + "é" * 300
        parts = live.chunks(s)
        self.assertEqual("".join(parts), s)
        self.assertTrue(all(len(p.encode("utf-8")) <= 500 for p in parts))


class ProtocolTests(unittest.TestCase):
    def test_delegation_is_mapped_once_after_user_transcript(self):
        p = live.Protocol()
        delta = p.from_provider({'type': 'input_transcript.added', 'item': {'text': 'Check this'}})
        self.assertEqual(delta[0]['type'], 'session.input_transcript.delta')
        event = {'type': 'delegation.created', 'item': {'target': 'client', 'id': 'd1',
                 'content': [{'type': 'input_text', 'text': 'Check this'}]}}
        out = p.from_provider(event)
        self.assertEqual(out[0]['delegation']['id'], 'd1')
        self.assertEqual(p.from_provider(event), [])
        self.assertEqual(p.from_provider({'type': 'turn.done', 'turn': {
            'role': 'user', 'transcript': 'Check this'}}), [])

    def test_final_result_and_quiet_progress_use_distinct_channels(self):
        p = live.Protocol()
        p.seen.append('d1')
        for kind, channel in [('session.thinking.append', 'commentary'),
                              ('session.commentary.append', 'speakable')]:
            out = p.from_mobile({'type': kind, 'delegation_id': 'd1', 'content': 'A real result'})
            self.assertEqual(out[0]['type'], 'delegation.context.append')
            self.assertEqual(out[0]['delegation_item_id'], 'd1')
            self.assertEqual(out[0]['channel'], channel)
        self.assertIn('Agent Final Message', out[0]['content'][0]['text'])

    def test_foreign_delegation_is_rejected(self):
        self.assertEqual(live.Protocol().from_mobile({'type': 'session.commentary.append',
                         'delegation_id': 'foreign', 'content': 'Wrong chat'}), [])

    def test_unicode_chunks_never_exceed_provider_byte_limit(self):
        text = '🌿你好' * 600
        chunks = live.chunks(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c.encode()) <= 500 for c in chunks))

    def test_fallback_transcript_precedes_delegation(self):
        p = live.Protocol()
        out = p.from_provider({'type': 'delegation.created', 'item': {'target': 'client', 'id': 'd1',
                               'content': [{'type': 'input_text', 'text': 'Check now'}]}})
        self.assertEqual(out[0]['delta'], 'Check now')
        self.assertEqual(out[1]['type'], 'session.delegation.created')


class HostDelegationTests(unittest.IsolatedAsyncioTestCase):
    """Android 1.0.0 crashes on session.delegation.created ((?U) regex), so delegation runs on the host."""

    def test_host_mode_never_sends_delegation_to_phone(self):
        p = live.Protocol(host_delegation=True)
        out = p.from_provider({'type': 'delegation.created', 'item': {'target': 'client', 'id': 'd1',
                               'content': [{'type': 'input_text', 'text': 'Check now'}]}})
        self.assertNotIn('session.delegation.created', [m['type'] for m in out])
        self.assertEqual(p.pending_host, [('d1', 'Check now')])

    async def test_host_result_reaches_provider_as_final_message(self):
        call = live.Call(live.Manager(None), profile='')
        call.protocol.seen.append('d1')
        sent = []
        call.send = lambda event, to_provider=False: sent.append((event, to_provider))
        with patch.object(live, 'run_hermes', return_value='It is 72 degrees.'):
            await call.host_delegate('d1', 'weather?')
        self.assertTrue(sent and all(tp for _, tp in sent))
        self.assertEqual(sent[0][0]['type'], 'delegation.context.append')
        self.assertEqual(sent[0][0]['delegation_item_id'], 'd1')
        self.assertIn('It is 72 degrees.', sent[0][0]['content'][0]['text'])
        await call.close('test')

    async def test_lane_answer_is_spoken_first_and_hermes_only_if_it_changes_it(self):
        if live.LaneRelay is None:
            self.skipTest("z0live checkout without mobile_relay (set Z0LIVE_DESKTOP)")

        class Relay:
            counts, turn = {}, {"text": "what is the capital of australia", "receipt": {"entry": 7}}

            async def answer(self, request):
                return "Canberra."

            async def close(self):
                pass

        for hermes, sends in (("The capital of Australia is Canberra.", 1), ("It is Sydney, actually.", 2)):
            call = live.Call(live.Manager(None), profile='')
            call.relay = Relay()
            call.protocol.seen.append('d1')
            sent = []
            call.send = lambda event, to_provider=False: sent.append((event, to_provider))
            with patch.object(live, 'run_hermes', return_value=hermes):
                await call.host_delegate('d1', 'what is the capital of australia')
            self.assertEqual(len(sent), sends, hermes)
            self.assertIn('Canberra.', sent[0][0]['content'][0]['text'])
            self.assertEqual(sent[0][0]['delegation_item_id'], 'd1')
            self.assertEqual(call.counts['lane_answer_spoken'], 1)
            call.relay = None
            await call.close('test')

    async def test_host_failure_is_spoken_not_silent(self):
        call = live.Call(live.Manager(None), profile='')
        call.protocol.seen.append('d1')
        sent = []
        call.send = lambda event, to_provider=False: sent.append((event, to_provider))
        with patch.object(live, 'run_hermes', side_effect=RuntimeError('boom')):
            await call.host_delegate('d1', 'weather?')
        self.assertIn("couldn't finish", sent[0][0]['content'][0]['text'])
        await call.close('test')

    def test_profile_name_rejects_paths(self):
        self.assertEqual(live.profile_name('../etc'), '')
        self.assertEqual(live.profile_name('--yolo'), '')
        self.assertEqual(live.profile_name('default'), '')


class SpeakFirstGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_is_silent_until_the_phone_sends_sound(self):
        import av

        class Source:
            def __init__(self, level):
                self.level = level

            async def recv(self):
                frame = av.AudioFrame.from_ndarray(__import__("numpy").full((1, 960), self.level, "int16"),
                                                   format="s16", layout="mono")
                frame.sample_rate = 48000
                return frame

        def peak(frame):
            return int(abs(frame.to_ndarray()).max())

        call = live.Call(live.Manager(None), profile='')
        try:
            said = json.dumps({"type": "output_transcript.added", "item": {"text": " Mm-h"}})
            call.message(said, True)
            self.assertFalse(any("output_transcript" in str(m.get("type")) for m in call.to_mobile))
            voice = live.AudioBridge(Source(5000), call, "output")
            self.assertEqual(peak(await voice.recv()), 0)  # the voice tried to speak first: silenced
            await live.AudioBridge(Source(0), call, "input").recv()
            self.assertEqual(peak(await voice.recv()), 0)  # phone silence does not open it
            await live.AudioBridge(Source(5000), call, "input").recv()
            self.assertGreater(peak(await voice.recv()), 0)  # Kevin spoke: the voice is heard from now on
            call.message(said, True)
            self.assertTrue(any("output_transcript" in str(m.get("type")) for m in call.to_mobile))
        finally:
            await call.close("test")


class VoiceSessionContinuityTests(unittest.TestCase):
    """Each voice turn must continue the profile's Hermes session, not start a new one."""
    def setUp(self):
        live.VOICE_SESSIONS.clear()

    def fake(self, outcomes):
        calls = []
        def run(args, **kw):
            calls.append(args)
            rc, out = outcomes.pop(0)
            # Real `hermes chat -Q` prints the answer on stdout and `session_id: ...` on stderr.
            text = [ln for ln in out.splitlines() if not ln.startswith('session_id:')]
            sid = [ln for ln in out.splitlines() if ln.startswith('session_id:')]
            return type('P', (), {'returncode': rc, 'stdout': '\n'.join(text), 'stderr': '\n'.join(sid)})()
        return calls, run

    def test_second_turn_resumes_first_session(self):
        calls, run = self.fake([(0, 'ok\nsession_id: s1'), (0, 'yes\nsession_id: s1')])
        with patch.object(live.subprocess, 'run', side_effect=run):
            self.assertEqual(live.run_hermes('remember X', 'clean'), 'ok')
            self.assertEqual(live.run_hermes('yes', 'clean'), 'yes')
        self.assertNotIn('--resume', calls[0])
        self.assertEqual(calls[1][-2:], ['--resume', 's1'])
        self.assertEqual(calls[0][calls[0].index('--source') + 1], 'oneshot')  # no --source -> invalid_params

    def test_idle_session_starts_fresh(self):
        live.VOICE_SESSIONS['clean'] = ('old', live.time.monotonic() - live.VOICE_SESSION_IDLE_SECONDS - 1)
        calls, run = self.fake([(0, 'hi\nsession_id: s2')])
        with patch.object(live.subprocess, 'run', side_effect=run):
            live.run_hermes('hello', 'clean')
        self.assertNotIn('--resume', calls[0])
        self.assertEqual(live.VOICE_SESSIONS['clean'][0], 's2')

    def test_failed_resume_falls_back_to_fresh_session(self):
        live.VOICE_SESSIONS['clean'] = ('gone', live.time.monotonic())
        calls, run = self.fake([(1, ''), (0, 'fine\nsession_id: s3')])
        with patch.object(live.subprocess, 'run', side_effect=run):
            self.assertEqual(live.run_hermes('hello', 'clean'), 'fine')
        self.assertIn('--resume', calls[0])
        self.assertNotIn('--resume', calls[1])
        self.assertEqual(live.VOICE_SESSIONS['clean'][0], 's3')


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Teardown stops the server with fixture calls still open; draining is covered by DrainTests.
        drain = patch.object(live, "DRAIN_SECONDS", 0)
        drain.start()
        self.addCleanup(drain.stop)
        self.peers = []
        self.tasks = []
        self.received = asyncio.Queue()
        self.provider_channels = []

        async def auth(request):
            if request.headers.get('Authorization') != 'Bearer fixture-valid':
                raise web.HTTPUnauthorized()
            return web.json_response({'user_id': 'fixture-user', 'provider': 'fixture'})

        async def signal(request):
            data = await request.json()
            self.assertEqual(data['session']['delegation'], {'type': 'client'})
            self.assertNotIn('tools', data['session'])
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.peers.append(peer)
            peer.addTrack(AudioStreamTrack())
            @peer.on('track')
            def track_received(track):
                async def drain():
                    try:
                        while True:
                            await track.recv()
                    except Exception:
                        pass
                self.tasks.append(asyncio.create_task(drain()))
            @peer.on('datachannel')
            def channel_received(channel):
                self.provider_channels.append(channel)
                @channel.on('message')
                def got(raw):
                    self.received.put_nowait(json.loads(raw))
                @channel.on('open')
                def opened():
                    channel.send(json.dumps({'type': 'session.started', 'session': {'id': 'fixture-live'}}))
                if channel.readyState == 'open':
                    opened()
            await peer.setRemoteDescription(RTCSessionDescription(sdp=data['sdp'], type='offer'))
            await peer.setLocalDescription(await peer.createAnswer())
            return web.Response(status=201, text=peer.localDescription.sdp, content_type='application/sdp')

        fake = web.Application()
        fake.router.add_get('/api/auth/me', auth)
        fake.router.add_post('/signal', signal)
        self.fake = TestServer(fake)
        await self.fake.start_server()
        self.patches = [patch.object(live, 'UPSTREAM', str(self.fake.make_url('/')).rstrip('/')),
                        patch.object(live, 'SIGNALING', str(self.fake.make_url('/signal'))),
                        patch.object(live, 'codex_headers', return_value={'Authorization': 'Bearer fixture-subscription'})]
        for p in self.patches:
            p.start()
        self.app = live.make_app()
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.http = ClientSession()

    async def asyncTearDown(self):
        await self.server.close()
        await asyncio.gather(*(p.close() for p in self.peers), return_exceptions=True)
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.http.close()
        await self.fake.close()
        for p in self.patches:
            p.stop()

    async def test_auth_missing_invalid_and_cookie_only_are_denied(self):
        for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Cookie': 'anything=1'}):
            async with self.http.get(self.server.make_url('/api/audio/voice-live/status'), headers=headers) as r:
                self.assertEqual(r.status, 401)
        async with self.http.post(self.server.make_url('/api/audio/voice-live/session'), json={'sdp': 'v=0'},
                                  headers={'Authorization': 'Bearer wrong'}) as r:
            self.assertEqual(r.status, 401)
        self.assertEqual(len(self.app['manager'].calls), 0)

    async def test_status_requires_real_identity_and_returns_no_secret(self):
        async with self.http.get(self.server.make_url('/api/audio/voice-live/status'),
                                  headers={'Authorization': 'Bearer fixture-valid'}) as r:
            data = await r.json()
        self.assertTrue(data['available'])
        self.assertEqual(data['model'], 'gpt-live-1-codex')
        self.assertNotIn('fixture-subscription', json.dumps(data))

    async def test_commands_wait_for_provider_session_started_not_merely_open_channel(self):
        class Channel:
            readyState = 'open'
            def __init__(self):self.sent = []
            def send(self, raw):self.sent.append(json.loads(raw))
        manager = live.Manager(self.http)
        call = live.Call(manager)
        manager.calls[call.id] = call
        channel = Channel()
        call.provider_dc = channel
        try:
            call.send({'type': 'session.context.append', 'content': []}, True)
            self.assertEqual(channel.sent, [])
            call.message(json.dumps({'type': 'session.started', 'session': {'id': 'fixture'}}), True)
            self.assertEqual(channel.sent[0]['type'], 'session.context.append')
            self.assertTrue(call.provider_ready)
        finally:
            await manager.close()

    @patch.object(live, 'HOST_DELEGATION', False)
    async def test_real_webrtc_audio_and_bidirectional_protocol_translation(self):
        trace_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(live, 'TRACE_DIR', trace_dir))
        self.enterContext(patch.dict(os.environ, {'HERMES_MOBILE_VOICE_TRACE': '1'}))
        client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(client)
        client.addTrack(AudioStreamTrack())
        dc = client.createDataChannel('oai-events')
        output = asyncio.Queue()
        @dc.on('message')
        def received(raw):
            output.put_nowait(json.loads(raw))
        @client.on('track')
        def track_received(track):
            async def drain():
                try:
                    while True:
                        await track.recv()
                except Exception:
                    pass
            self.tasks.append(asyncio.create_task(drain()))
        await client.setLocalDescription(await client.createOffer())
        async with self.http.post(self.server.make_url('/api/audio/voice-live/session'),
                json={'sdp': client.localDescription.sdp}, headers={'Authorization': 'Bearer fixture-valid'}) as r:
            self.assertEqual(r.status, 200)
            result = await r.json()
        await client.setRemoteDescription(RTCSessionDescription(sdp=result['transport']['sdp'], type='answer'))
        async with asyncio.timeout(10):
            while dc.readyState != 'open' or not self.provider_channels or self.provider_channels[0].readyState != 'open':
                await asyncio.sleep(.05)
            provider = self.provider_channels[0]
            provider.send(json.dumps({'type': 'input_transcript.added', 'item': {'text': 'Check the fixture'}}))
            provider.send(json.dumps({'type': 'delegation.created', 'item': {'target': 'client', 'id': 'd1',
                         'content': [{'type': 'input_text', 'text': 'Check the fixture'}]}}))
            while (await output.get()).get('type') != 'session.delegation.created':
                pass
            dc.send(json.dumps({'type': 'session.commentary.append', 'delegation_id': 'd1', 'content': 'Fixture passed'}))
            event = await self.received.get()
            while event['type'] == 'session.context.append':  # the call brief arrives first (11-16)
                event = await self.received.get()
            self.assertEqual(event['type'], 'delegation.context.append')
            self.assertEqual(event['channel'], 'speakable')
            self.assertEqual(event['delegation_item_id'], 'd1')
            call = self.app['manager'].calls[result['session']['id']]
            while not(call.counts['input_frames'] > 3 and call.counts['output_frames'] > 3):
                await asyncio.sleep(.05)
            dc.send(json.dumps({'type': 'session.input_audio.mute'}))
            await asyncio.sleep(.1)
            self.assertTrue(call.muted)
            dc.send(json.dumps({'type': 'session.close'}))
            while self.app['manager'].calls:
                await asyncio.sleep(.05)
        self.assertEqual(self.app['manager'].recent[-1]['reason'], 'client_closed')
        trace, = trace_dir.glob('*/*.jsonl')
        context = [row for row in map(json.loads, trace.read_text().splitlines()) if row['ev'] == 'context.sent']
        setup, = [row for row in context if row['type'] == 'signaling.session']
        self.assertEqual(setup['turn_id'], 0)
        self.assertEqual(setup['instructions_sha256'], hashlib.sha256(live.PERSONA.encode()).hexdigest())
        self.assertTrue(any(row['type'] == 'session.context.append' and row['turn_id'] == 0 for row in context))
        reply, = [row for row in context if row.get('did') == 'd1']
        self.assertEqual(reply['turn_id'], 1)
        self.assertEqual(reply['texts'], [event['content'][0]['text']])



class OneFastLaneTests(unittest.IsolatedAsyncioTestCase):
    """L2 (11-11): one fast-lane system. The z0live lanes (smol + the tool-using gather lane) race the
    driver, which starts at once; there is no second, adapter-owned liaison any more."""

    class Relay:
        def __init__(self, text, delay, source=None):
            self.text, self.delay, self.cancelled = text, delay, False
            self.counts, self.turn = {}, {"text": "which branch is z0live on", "receipt": {}, **({"source": source} if source else {})}

        async def answer(self, request):
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return self.text

        async def close(self):
            pass

    async def delegate(self, relay, driver_s, answer="The z0live checkout is on a detached HEAD (2dd8589)."):
        flags, sent = [], []
        def run_hermes(request, profile, own_lane=False):
            flags.append(own_lane)
            live.time.sleep(driver_s)
            return answer
        call = live.Call(live.Manager(None), profile='clean')
        call.relay = relay
        call.protocol.seen.append('d1')
        call.send = lambda event, to_provider=False: event.get('content') and sent.append(
            event['content'][0]['text'].split('\n\n', 1)[-1])  # minus the protocol's message header
        began = live.time.monotonic()
        with patch.object(live, 'run_hermes', run_hermes):
            await call.host_delegate('d1', 'which branch is z0live on')
        call.relay = None
        await call.close('test')
        return call, flags, sent, live.time.monotonic() - began

    async def test_gather_answer_is_framed_and_spoken_while_the_driver_runs(self):
        relay = self.Relay("You're on a detached HEAD at 2dd8589.", 0.1, source="gather")
        call, flags, sent, took = await self.delegate(relay, 0.6)
        self.assertEqual(flags, [True])  # FAST_LANE_OFF: the profile plugin starts no second lane
        self.assertEqual(sent[0], "You're on a detached HEAD at 2dd8589.")  # no plumbing framing (11-16)
        self.assertIn('detached HEAD (2dd8589)', sent[-1])
        self.assertLess(took, 1.2)  # the lane wait never runs before the driver (it used to add up to 9.5 s)

    async def test_driver_first_cancels_the_lane_and_speaks_once(self):
        relay = self.Relay("late lane", 2.0)
        call, flags, sent, took = await self.delegate(relay, 0.1)
        self.assertTrue(relay.cancelled)
        self.assertEqual(len(sent), 1)
        self.assertNotIn('late lane', sent[0])
        self.assertLess(took, 1.0)

    async def test_unhelpful_gather_answer_is_not_spoken(self):
        relay = self.Relay("I couldn't find anything about that.", 0.05, source="gather")
        call, flags, sent, _ = await self.delegate(relay, 0.3)
        self.assertEqual(call.counts['lane_unhelpful'], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn('detached HEAD', sent[0])

    async def test_smol_answer_is_plain_and_driver_only_if_it_adds(self):
        relay = self.Relay("Canberra.", 0.05)
        call, _, sent, _ = await self.delegate(relay, 0.2, answer="The capital of Australia is Canberra.")
        self.assertEqual(sent, ["Canberra."])
        self.assertEqual(call.counts['host_delegation_covered'], 1)

    async def test_lane_layout_line_is_answered_without_the_driver(self):
        relay = self.Relay(None, 0)
        relay.turn["receipt"] = {"configured": {"ok": True, "say": "Two fast lanes, one large."}}
        call, flags, sent, _ = await self.delegate(relay, 0.1)
        self.assertEqual(flags, [])
        self.assertEqual(sent, ["Two fast lanes, one large."])

    async def test_late_lane_layout_receipt_still_skips_the_driver_answer(self):
        relay = self.Relay("Two fast lanes, one large.", 0.05)
        relay.turn["receipt"] = None  # the broker receipt is still in flight when the voice delegates
        inner = relay.answer
        async def answer(request):
            text = await inner(request)
            relay.turn["receipt"] = {"configured": {"ok": True, "say": text}}
            return text
        relay.answer = answer
        call, flags, sent, _ = await self.delegate(relay, 0.3)
        self.assertEqual(sent, ["Two fast lanes, one large."])
        self.assertEqual(call.counts['host_delegation_layout_late'], 1)

    def test_driver_env_disables_its_own_fast_lane_only_when_lanes_run(self):
        seen = []
        def run(args, **kw):
            seen.append(kw.get('env'))
            return type('P', (), {'returncode': 0, 'stdout': 'ok', 'stderr': 'session_id: s'})()
        with patch.object(live.subprocess, 'run', side_effect=run):
            live._hermes_once('q', 'clean', '', True)
            live._hermes_once('q', 'clean', '', False)
        self.assertEqual(seen[0]['FAST_LANE_OFF'], '1')
        self.assertIsNone(seen[1])


class DrainTests(unittest.IsolatedAsyncioTestCase):
    """A service stop/restart must never cut an open call (it did, twice, on 2026-11-06)."""
    async def test_shutdown_waits_for_open_call_and_refuses_new_ones(self):
        closed = []
        class FakeCall:
            def __init__(self, manager):
                self.manager = manager
            async def close(self, reason):
                closed.append(reason)
                self.manager.calls.pop("c1", None)
        manager = live.Manager(None)
        manager.calls["c1"] = call = FakeCall(manager)
        stopping = asyncio.ensure_future(manager.close())
        await asyncio.sleep(1.5)
        self.assertFalse(stopping.done())          # still serving the open call
        self.assertEqual(closed, [])
        self.assertTrue(manager.draining)          # and admits no new one
        await call.close("client_closed")          # the user hangs up
        await asyncio.wait_for(stopping, 3)
        self.assertEqual(closed, ["client_closed"])

    async def test_drain_has_a_bound(self):
        closed = []
        class FakeCall:
            async def close(self, reason):
                closed.append(reason)
        manager = live.Manager(None)
        manager.calls["c1"] = FakeCall()
        with patch.object(live, "DRAIN_SECONDS", 0.2):
            await asyncio.wait_for(manager.close(), 3)
        self.assertEqual(closed, ["service_shutdown"])


if __name__ == '__main__':
    unittest.main()
