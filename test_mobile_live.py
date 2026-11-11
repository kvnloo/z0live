"""Deterministic protocol/auth tests and real localhost WebRTC fixture tests."""
import asyncio
import json
import os
import unittest

# Unit tests never spawn a real liaison engine (fx/hermes); tests that need one patch live.liaison.
os.environ.setdefault('HERMES_MOBILE_VOICE_LIAISON', '0')
from unittest.mock import patch

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack

import mobile_live as live


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
            counts, turn = {}, {"receipt": {"entry": 7}}

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



class FastLaneVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_answer_is_spoken_before_full_answer(self):
        import tempfile, types
        from collections import Counter
        from pathlib import Path
        said = []
        call = types.SimpleNamespace(id="c1", closed=False, counts=Counter(),
                                     speak_for=lambda did, text: said.append((did, text)))
        with tempfile.TemporaryDirectory() as d:
            spool = Path(d) / "a.json"
            async def hermes():
                await asyncio.sleep(0.2)
                spool.write_text(json.dumps({"answer": "9168", "model": "gpt-6-luna"}))
                await asyncio.sleep(1.5)
                return "full"
            job = asyncio.ensure_future(hermes())
            await live.Call.speak_fast_lane(call, "d1", spool, job)
            self.assertFalse(job.done())          # spoken while the full answer was still running
            self.assertEqual(said, [("d1", "Quick first look, still being checked: 9168")])
            self.assertFalse(spool.exists())
            await job

    async def test_not_found_quick_answer_is_never_spoken(self):
        import tempfile, types
        from collections import Counter
        from pathlib import Path
        said = []
        call = types.SimpleNamespace(id="c1", closed=False, counts=Counter(),
                                     speak_for=lambda did, text: said.append((did, text)))
        with tempfile.TemporaryDirectory() as d:
            spool = Path(d) / "a.json"
            spool.write_text(json.dumps({"answer": "Not found in the local workspace.", "model": "gpt-6-luna"}))
            job = asyncio.ensure_future(asyncio.sleep(0.3))
            await live.Call.speak_fast_lane(call, "d1", spool, job)
            self.assertEqual(said, [])
            self.assertEqual(call.counts["fast_lane_unhelpful"], 1)
            await job

    async def test_liaison_answers_first_and_driver_runs_without_its_own_lane(self):
        """Voice/driver split: the adapter starts the liaison at delegation, before the driver's cold start."""
        import tempfile, threading, types
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d)

            class Lane:
                def __init__(self, parent, text, key):
                    self.key, self.stopped = key, False
                def watch(self):  # the real plugin's spool thread writes the answer when it exists
                    threading.Timer(0.1, lambda: (logs / "answers" / f"{self.key}.json").write_text(
                        json.dumps({"answer": "detached HEAD at 2dd8589", "model": "gpt-6-luna"}))).start()
                def stop(self):
                    self.stopped = True
            fl = types.SimpleNamespace(LOGS=logs, _lock=threading.Lock(), _lanes={}, Lane=Lane,
                                       answer_key=lambda q: "k1", brief=lambda q: "brief " + q)
            driver = []
            def run_hermes(request, profile, own_lane=False):
                driver.append(own_lane)
                live.time.sleep(0.6)
                return "The z0live checkout is on a detached HEAD (2dd8589)."
            call = live.Call(live.Manager(None), profile='ayo')
            call.relay = None
            call.protocol.seen.append('d1')
            sent = []
            call.send = lambda event, to_provider=False: event.get('content') and sent.append(event['content'][0]['text'])
            with patch.object(live, 'liaison', return_value=fl), patch.object(live, 'run_hermes', run_hermes):
                await call.host_delegate('d1', 'which branch is z0live on')
            self.assertEqual(driver, [True])  # FAST_LANE_OFF for the driver: one liaison, not two
            self.assertIn('Quick first look', sent[0])
            self.assertIn('2dd8589', sent[0])
            self.assertIn('detached HEAD (2dd8589)', sent[-1])  # the driver's answer still follows
            self.assertTrue(fl._lanes['%s' % call.id].stopped)
            self.assertEqual(call.counts['liaison_started'], 1)
            await call.close('test')

    async def test_liaison_off_keeps_plugin_spool_path(self):
        driver = []
        call = live.Call(live.Manager(None), profile='')
        call.relay = None
        call.protocol.seen.append('d1')
        call.send = lambda event, to_provider=False: None
        def run_hermes(request, profile, own_lane=False):
            driver.append(own_lane)
            return 'ok'
        with patch.object(live, 'liaison', return_value=None), patch.object(live, 'run_hermes', run_hermes):
            await call.host_delegate('d1', 'q')
        self.assertEqual(driver, [False])
        await call.close('test')

    def test_driver_env_disables_its_own_fast_lane_only_when_adapter_runs_one(self):
        seen = []
        def run(args, **kw):
            seen.append(kw.get('env'))
            return type('P', (), {'returncode': 0, 'stdout': 'ok', 'stderr': 'session_id: s'})()
        with patch.object(live.subprocess, 'run', side_effect=run):
            live._hermes_once('q', 'clean', '', True)
            live._hermes_once('q', 'clean', '', False)
        self.assertEqual(seen[0]['FAST_LANE_OFF'], '1')
        self.assertIsNone(seen[1])

    def test_spool_key_matches_plugin(self):
        import hashlib
        p = live.fast_lane_spool("  what   port\n", "clean")
        self.assertEqual(p.name, hashlib.sha1(b"what port").hexdigest() + ".json")
        self.assertEqual(p.parent.parts[-4:], ("clean", "cache", "fast-lane", "answers"))


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
