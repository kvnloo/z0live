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
from unittest.mock import MagicMock, patch

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config (~/.z0int)
import mobile_live as live  # noqa: E402


def setUpModule():
    config = patch.object(live.guest_voice, "configured", return_value=None)
    config.start()
    unittest.addModuleCleanup(config.stop)


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

    def test_tamil_chunks_use_utf8_bytes_not_code_points(self):
        text = "தமிழ்" * 40
        self.assertEqual((len(text), len(text.encode("utf-8"))), (200, 600))

        parts = live.chunks(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 500 for part in parts))


class HandoffPreambleTests(unittest.TestCase):
    """Takes 7 and 9: the voice's own hand-off line promised new work Hermes then did not start."""

    def test_take_7_and_9_handoff_lines_count_as_promises_and_neutral_lines_do_not(self):
        for line in ("Got it, I'll handle that and keep receipts.",
                     "Got it, I\u2019m taking that on end-to-end. I\u2019ll be back with receipts once it\u2019s all "
                     "buttoned up.", "On it.", "Understood, I'll handle it end-to-end and show results when it's done.",
                     "Got it\u2014I\u2019ll go step by step and show receipts.",  # take 14
                     "Mm-hm. I'll take that. You're right, staging's moved on.", "I\u2019ll take it.",  # take 15
                     "Still working on that in Hermes. I'll tell you as soon as it's done; ask anything else meanwhile.",
                     "I'll let you know when it's done."):
            self.assertTrue(live.promises_new_work(line), line)
        for line in ("Let me check.", "Checking now.", "I'll take a quick look.", "Mm-hm.",
                     "I'll check your laptop quickly.", live.RECEIPT, live.QUEUED.format(running=3, position=1)):
            self.assertFalse(live.promises_new_work(line), line)

    def test_persona_asks_for_a_neutral_handoff_and_leaves_the_outcome_to_the_client(self):
        persona = " ".join(live.PERSONA.split())
        self.assertIn('such as "Let me check."', persona)
        self.assertFalse(live.promises_new_work("Let me check."))
        for rule in ("never promise new work", "taking it on or handling it", "promise receipts or a follow-up",
                     "The client's answer, queued notice or refusal says what happened"):
            self.assertIn(rule, persona)


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
            self.assertGreater(peak(await voice.recv()), 0)  # the owner spoke: the voice is heard from now on
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

    def test_failed_resume_does_not_repeat_a_possibly_executed_request(self):
        live.VOICE_SESSIONS['clean'] = ('old', live.time.monotonic())
        calls, run = self.fake([(1, '')])
        with patch.object(live.subprocess, 'run', side_effect=run), self.assertRaises(RuntimeError):
            live.run_hermes('run this once', 'clean')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ['--resume', 'old'])


class ConcurrentHostTurnTests(unittest.IsolatedAsyncioTestCase):
    """Take 7 of the 11-22 film call: The owner's long-run ask never reached Hermes during the call. It waited behind the
    call's running request (one turn at a time per call), while the voice said "Got it, I'll handle that"."""

    def setUp(self):
        live.VOICE_SESSIONS.clear()
        self.release = __import__("threading").Event()
        self.runs = []  # (args, monotonic start) of each Hermes process

    def hermes(self, args, **kw):
        self.runs.append((args, live.time.monotonic()))
        n = len(self.runs)
        if n == 1:
            self.release.wait(10)  # the first request is a long run
        return type('P', (), {'returncode': 0, 'stdout': f'answer {n}', 'stderr': f'session_id: s{n}'})()

    async def call(self):
        call = live.Call(live.Manager(None), profile='clean')
        sent = []
        call.send = lambda event, to_provider=False, **kw: sent.append(event)
        self.addAsyncCleanup(call.close, 'test')
        return call, sent

    async def test_a_new_request_reaches_hermes_while_an_earlier_one_runs(self):
        call, sent = await self.call()
        call.protocol.seen.extend(['d1', 'd2'])
        with patch.object(live.subprocess, 'run', side_effect=self.hermes), patch.object(live, 'RECEIPT_SECONDS', 30):
            first = asyncio.ensure_future(call.host_delegate('d1', "What's on my phone versus staging?"))
            await asyncio.sleep(0.2)
            began = live.time.monotonic()
            second = asyncio.ensure_future(call.host_delegate('d2', 'Get all of it onto my S25'))
            for _ in range(50):
                if len(self.runs) == 2:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(len(self.runs), 2, "the second request waited for the first")
            self.assertLess(self.runs[1][1] - began, 1.0)
            self.assertNotIn('--resume', self.runs[1][0], "a session busy with a turn is never resumed twice")
            await asyncio.wait_for(second, 5)
            self.assertTrue(any('answer 2' in str(e) for e in sent))
            self.release.set()
            await asyncio.wait_for(first, 5)
        self.assertEqual(live.VOICE_SESSIONS['clean'][0], 's1', "the voice session stays the first request's")

    async def test_past_the_bound_a_request_is_queued_and_the_voice_says_so(self):
        call, sent = await self.call()
        dids = [f'd{i}' for i in range(live.HOST_MAX + 1)]
        call.protocol.seen.extend(dids)

        def busy(request, profile, own_lane=False):
            self.runs.append((request, live.time.monotonic()))
            self.release.wait(10)
            return 'done: ' + request

        with patch.object(live, 'run_hermes', busy), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = [asyncio.ensure_future(call.host_delegate(did, f'request {did}')) for did in dids]
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.runs), live.HOST_MAX)
            said = [str(e) for e in sent]
            self.assertTrue(any('queued' in s and f'd{live.HOST_MAX}' in s for s in said), said)
            self.release.set()
            await asyncio.wait_for(asyncio.gather(*jobs), 5)
        self.assertEqual(len(self.runs), live.HOST_MAX + 1)


class NoLinkedChatTests(unittest.IsolatedAsyncioTestCase):
    """112025 (11-27 11:20): chat.resolved "" sent all three requests to cold Hermes CLI runs; none answered in the
    61 s left. A call with the app's bearer but no matched chat runs them as background agents beside the profile's
    newest chat; the CLI is only for when the dashboard cannot be reached."""

    async def call(self):
        call = live.Call(live.Manager(None), profile='tek')
        call.token = 'tok'
        sent = []
        call.send = lambda event, to_provider=False, **kw: sent.append(event)
        self.addAsyncCleanup(call.close, 'test')
        call.protocol.seen.append('d1')
        return call, sent

    async def test_no_matched_chat_runs_a_background_agent_not_the_cli(self):
        call, sent = await self.call()
        asked = []

        async def background(http, token, profile, sid, request, on_receipt=None):
            asked.append((sid, request))
            return 'The garden is sunny and twenty degrees.'

        cli = MagicMock(side_effect=AssertionError("a cold CLI run on a call"))
        with patch.object(live.chat_continuity, 'anchor_chat', return_value=('gui9', 'recent_user')), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live, 'run_hermes', cli), patch.object(live, 'RECEIPT_SECONDS', 30):
            await asyncio.wait_for(call.host_delegate('d1', 'what is the weather in the garden'), 5)
        cli.assert_not_called()
        self.assertEqual([sid for sid, _ in asked], ['gui9'])
        self.assertIn('what is the weather in the garden', asked[0][1])
        self.assertTrue(any('sunny and twenty degrees' in str(e) for e in sent))

    async def test_an_unreachable_dashboard_falls_back_to_the_cli(self):
        call, sent = await self.call()

        async def background(*a, **k):
            raise OSError("connection refused")

        with patch.object(live.chat_continuity, 'anchor_chat', return_value=('gui9', 'recent_user')), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live, 'run_hermes', return_value='cli answer'), patch.object(live, 'RECEIPT_SECONDS', 30):
            await asyncio.wait_for(call.host_delegate('d1', 'what is the weather in the garden'), 5)
        self.assertTrue(any('cli answer' in str(e) for e in sent))


    async def test_a_background_agent_that_times_out_is_not_run_again_on_the_cli(self):
        call, sent = await self.call()

        async def background(*a, **k):
            raise asyncio.TimeoutError()  # TimeoutError is an OSError: it must not look like an unreachable dashboard

        cli = MagicMock(return_value='second run')
        with patch.object(live.chat_continuity, 'anchor_chat', return_value=('gui9', 'recent_user')), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live, 'run_hermes', cli), patch.object(live, 'RECEIPT_SECONDS', 30):
            await asyncio.wait_for(call.host_delegate('d1', 'what is the weather in the garden'), 5)
        cli.assert_not_called()
        self.assertTrue(any("couldn" in str(e) and "finish" in str(e) for e in sent), sent)


    async def test_a_refused_background_run_falls_back_to_the_idle_chat_turn_then_the_cli(self):
        call, sent = await self.call()
        call.chat = 'chat1'
        turns = []

        async def refused(*a, **k):
            raise live.chat_continuity.NotStarted("prompt.background: agent still starting")

        async def ask_chat(http, token, profile, sid, request, timeout, only_if_idle=False):
            turns.append((sid, only_if_idle))
            return 'chat turn answer'

        with patch.object(live.chat_continuity, 'ask_background', refused), \
                patch.object(live.chat_continuity, 'ask_chat', ask_chat), \
                patch.object(live, 'run_hermes', MagicMock(side_effect=AssertionError("CLI"))), \
                patch.object(live, 'RECEIPT_SECONDS', 30):
            await asyncio.wait_for(call.host_delegate('d1', 'what is on the TV'), 5)
        self.assertEqual(turns, [('chat1', True)], "only an idle-checked chat turn")
        self.assertTrue(any('chat turn answer' in str(e) for e in sent))

        call2, sent2 = await self.call()  # no linked chat: the anchor refused -> the CLI, never a silent failure
        with patch.object(live.chat_continuity, 'anchor_chat', return_value=('gui9', 'recent_user')), \
                patch.object(live.chat_continuity, 'ask_background', refused), \
                patch.object(live, 'run_hermes', return_value='cli answer'), patch.object(live, 'RECEIPT_SECONDS', 30):
            await asyncio.wait_for(call2.host_delegate('d1', 'what is on the TV'), 5)
        self.assertTrue(any('cli answer' in str(e) for e in sent2))


class CrossBriefTests(unittest.IsolatedAsyncioTestCase):
    """The call opens with a short brief of the owner's other threads (call_brief.cross_thread); background
    agents get it too. A slow store never holds the call up."""

    async def test_the_brief_reaches_background_agents_and_a_slow_store_is_skipped(self):
        call = live.Call(live.Manager(None), profile='tek')
        self.addAsyncCleanup(call.close, 'test')
        traced, trace = [], call.trace

        class Recorder:  # the call's Trace, recording what it is handed
            def __call__(self, kind, **kw):
                traced.append((kind, kw))

            def __getattr__(self, name):
                return getattr(trace, name)

        call.trace = Recorder()
        loop = asyncio.get_running_loop()
        call.cross_lookup = loop.create_future()
        call.cross_lookup.set_result("Across the owner's threads (read-only snapshot taken at call start):\nCards running: x")
        call.cross_brief = await call.read_cross_brief()
        self.assertIn("Cards running: x", call.host_context(None, ledger=False))
        self.assertIn(("call.cross_brief", {"chars": len(call.cross_brief)}), traced)

        call.cross_lookup = loop.create_future()  # never resolves
        with patch.object(live, 'CROSS_BRIEF_WAIT', 0.05):
            self.assertEqual(await asyncio.wait_for(call.read_cross_brief(), 2), "")
        self.assertEqual(traced[-1], ("call.cross_brief", {"chars": 0, "error": "TimeoutError"}))


class StatusLineTests(unittest.TestCase):
    def test_status_lines_are_recognised_and_promise_nothing(self):
        lines = [live.RECEIPT, live.PROGRESS.format(seconds=30), live.CAP.format(seconds=90, where=live.WHERE["chat"]),
                 live.CAP.format(seconds=90, where=live.WHERE["background"])]
        for line in lines:
            self.assertTrue(live.status_line(line), line)
            self.assertFalse(live.promises_new_work(line), line)
        self.assertFalse(live.status_line("The dentist is at nine thirty."))
        self.assertTrue(live.status_line("seconds so far.", part=True))


class BoundedHostQueueTests(unittest.IsolatedAsyncioTestCase):
    """The host path's queue past HOST_MAX: bounded, truthful about what has started, and each answer lands on its
    own request. Each Hermes run below blocks on its own gate, so the tests decide which run ends when."""

    def setUp(self):
        live.VOICE_SESSIONS.clear()
        self.gates, self.started = {}, []

    def fake(self, request, profile, own_lane=False):
        did = request.split()[-1]
        self.started.append(did)
        gate = self.gates.setdefault(did, __import__("threading").Event())
        gate.wait(10)
        if did.startswith("fail"):
            raise RuntimeError("hermes exited 1")
        return f"answer for {did}"

    async def call(self):
        call = live.Call(live.Manager(None), profile='clean')
        self.sent = []
        call.send = lambda event, to_provider=False, **kw: self.sent.append(event)
        self.addAsyncCleanup(call.close, 'test')
        return call

    def said(self, did=None):
        out = []
        for e in self.sent:
            if did is None or e.get("delegation_item_id") == did:
                out.append("".join(c.get("text", "") for c in e.get("content", [])))
        return out

    async def until(self, check, s=3.0):
        for _ in range(int(s / 0.02)):
            if check():
                return True
            await asyncio.sleep(0.02)
        return check()

    def release(self, did):
        self.gates.setdefault(did, __import__("threading").Event()).set()

    async def start(self, call, dids):
        jobs = {}
        for did in dids:
            call.protocol.seen.append(did)
            jobs[did] = asyncio.ensure_future(call.host_delegate(did, f"request {did}"))
            await asyncio.sleep(0.02)
        return jobs

    async def test_past_the_queue_bound_a_request_is_refused_out_loud_and_never_runs(self):
        call = await self.call()
        running = [f"r{i}" for i in range(live.HOST_MAX)]
        waiting = [f"w{i}" for i in range(getattr(live, "QUEUE_MAX", 3))]  # the bound: 3 waiting
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, running + waiting + ["over"])
            self.assertTrue(await self.until(lambda: jobs["over"].done()), "a request past the bound waits silently")
            said = " ".join(self.said("over"))
            self.assertIn("can't take", said)
            self.assertNotIn("may still be running", said, "it never started, so it cannot still be running")
            self.assertNotIn("over", self.started)
            for did in running + waiting:
                self.release(did)
            await asyncio.wait_for(asyncio.gather(*jobs.values()), 5)
        self.assertNotIn("over", self.started, "a refused request is not run later either")
        self.assertEqual(sorted(self.started), sorted(running + waiting))

    async def test_queue_positions_are_spoken_and_runs_start_in_order(self):
        call = await self.call()
        running = [f"r{i}" for i in range(live.HOST_MAX)]
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, running + ["w1", "w2"])
            self.assertTrue(any("number 1" in s for s in self.said("w1")), self.said("w1"))
            self.assertTrue(any("number 2" in s for s in self.said("w2")), self.said("w2"))
            for did in running:
                self.assertFalse(any("queued" in s for s in self.said(did)), "an admitted request is never 'queued'")
            self.release("r1")
            self.assertTrue(await self.until(lambda: "w1" in self.started))
            await asyncio.sleep(0.1)
            self.assertNotIn("w2", self.started, "the bound holds: only one slot was freed")
            self.release("r0")
            self.assertTrue(await self.until(lambda: "w2" in self.started))
            for did in running + ["w1", "w2"]:
                self.release(did)
            await asyncio.wait_for(asyncio.gather(*jobs.values()), 5)
        self.assertEqual(self.started[-2:], ["w1", "w2"])

    async def test_each_answer_lands_on_its_own_request_when_runs_end_out_of_order(self):
        call = await self.call()
        dids = ["a", "b", "c"]
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, dids)
            for did in reversed(dids):
                self.release(did)
                await asyncio.wait_for(jobs[did], 5)
        for did in dids:
            answers = [s for s in self.said(did) if "answer for" in s]
            self.assertEqual(len(answers), 1, (did, self.said(did)))
            self.assertIn(f"answer for {did}", answers[0])

    async def test_a_failed_run_says_so_and_frees_its_slot_for_the_next_in_line(self):
        call = await self.call()
        running = ["fail0"] + [f"r{i}" for i in range(1, live.HOST_MAX)]
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, running + ["w1"])
            self.release("fail0")
            await asyncio.wait_for(jobs["fail0"], 5)
            self.assertTrue(any("couldn't finish" in s for s in self.said("fail0")))
            self.assertTrue(await self.until(lambda: "w1" in self.started), "a failure kept its slot")
            for did in running + ["w1"]:
                self.release(did)
            await asyncio.wait_for(asyncio.gather(*jobs.values()), 5)
        self.assertTrue(any("answer for w1" in s for s in self.said("w1")))

    async def test_a_cancelled_queued_request_never_runs_and_the_line_moves_up(self):
        call = await self.call()
        running = [f"r{i}" for i in range(live.HOST_MAX)]
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, running + ["w1", "w2"])
            # The unit a drop or a close cancels: the request's own Hermes job, while it waits at the head of the line.
            job = next(j for j in call.pending_delegations if j.get_coro().cr_frame.f_locals.get("did") == "w1")
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            self.release("r0")
            self.assertTrue(await self.until(lambda: "w2" in self.started), "the line did not move past a cancel")
            self.assertNotIn("w1", self.started)
            for did in running + ["w2"]:
                self.release(did)
            await asyncio.wait_for(asyncio.gather(*(j for d, j in jobs.items() if d != "w1")), 5)
        self.assertNotIn("w1", self.started)

    async def test_a_closed_call_runs_none_of_its_queued_requests(self):
        call = await self.call()
        running = [f"r{i}" for i in range(live.HOST_MAX)]
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 30):
            jobs = await self.start(call, running + ["w1"])
            await call.close('test')
            for did in running:
                self.release(did)
            await asyncio.gather(*jobs.values(), return_exceptions=True)
            await asyncio.sleep(0.1)
        self.assertNotIn("w1", self.started, "a request queued on a closed call still ran Hermes")

    async def test_a_queued_request_gets_no_still_working_receipt(self):
        call = await self.call()
        running = [f"r{i}" for i in range(live.HOST_MAX)]
        # The queued notice is a line handed to the voice: status lines wait HANDED_SECONDS after it (lane cards' gate).
        # STATUS_GAP 0: the queued notice is itself a status line in the call's budget (task I).
        with patch.object(live, 'run_hermes', self.fake), patch.object(live, 'RECEIPT_SECONDS', 0.2), \
                patch.object(live.lane_cards, 'HANDED_SECONDS', 0.1), patch.object(live, 'STATUS_GAP', 0):
            jobs = await self.start(call, running + ["w1"])
            await asyncio.sleep(0.8)
            self.assertFalse(any(live.status_line(s) for s in self.said("w1")), "the receipt claims a queued run works")
            self.assertFalse(any(live.status_line(s) for d in running for s in self.said(d)),
                             "a running request without a finding also gives no stock receipt")
            for did in running + ["w1"]:
                self.release(did)
            await asyncio.wait_for(asyncio.gather(*jobs.values()), 5)


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
        call.subscription.channel = channel
        try:
            call.send({'type': 'session.context.append', 'content': []}, True)
            self.assertEqual(channel.sent, [])
            call.message(json.dumps({'type': 'session.started', 'session': {'id': 'fixture'}}), True)
            self.assertEqual(channel.sent[0]['type'], 'session.context.append')
            self.assertTrue(call.provider_ready)
        finally:
            await manager.close()

    async def test_renewal_replays_brief_and_recap_without_renegotiating_phone(self):
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(phone)
        phone.addTrack(AudioStreamTrack())
        dc = phone.createDataChannel('oai-events')
        incoming = asyncio.Queue()
        dc.on('message', lambda raw: incoming.put_nowait(json.loads(raw)))
        await phone.setLocalDescription(await phone.createOffer())
        manager = self.app['manager']
        call = live.Call(manager)
        manager.calls[call.id] = call
        result = await call.start(phone.localDescription.sdp, [
            {'role': 'user', 'content': [{'text': 'The call brief fixture is purple'}]}])
        await phone.setRemoteDescription(RTCSessionDescription(sdp=result['transport']['sdp'], type='answer'))
        async with asyncio.timeout(10):
            while dc.readyState != 'open' or not self.provider_channels:
                await asyncio.sleep(.01)
            old = self.provider_channels[0]
            old.send(json.dumps({'type': 'turn.done', 'turn': {'role': 'user', 'transcript': 'Remember violet'}}))
            while (await incoming.get()).get('delta') != 'Remember violet':
                pass
            while not self.received.empty():
                self.received.get_nowait()
            await call.replace_subscription(live.time.monotonic() + 5)
            new = self.provider_channels[1]
            dc.send(json.dumps({'type': 'session.instructions.append', 'content': 'Phone is still connected'}))
            context = ""
            while 'Phone is still connected' not in context:
                event = await self.received.get()
                context += ' '.join(c.get('text', '') for c in event.get('content', []))
            self.assertEqual(new.readyState, 'open')
            self.assertIn('Remember violet', context)
            self.assertIn('The call brief fixture is purple', context)
            self.assertIn('Continuing the same call', context)
            self.assertEqual(phone.connectionState, 'connected')
            self.assertEqual(dc.readyState, 'open')
            self.assertFalse(call.closed)

    @patch.object(live, 'TIERS_ON', True)
    async def test_hermes_answer_still_running_at_renewal_reaches_the_new_session(self):
        # Renewal waits for hand-offs only until deadline - 2 s; delegations run p50 40 s, p90 166 s.
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(phone)
        phone.addTrack(AudioStreamTrack())
        dc = phone.createDataChannel('oai-events')
        await phone.setLocalDescription(await phone.createOffer())
        manager = self.app['manager']
        call = live.Call(manager)
        manager.calls[call.id] = call
        release = asyncio.Event()

        async def hermes(did, request):
            await release.wait()
            return 'The deploy log shows a clean exit.'

        async def no_lanes(did, request):
            return None

        call.ask_hermes, call.tier_lanes, call.relay = hermes, no_lanes, None
        result = await call.start(phone.localDescription.sdp, [])
        await phone.setRemoteDescription(RTCSessionDescription(sdp=result['transport']['sdp'], type='answer'))
        async with asyncio.timeout(15):
            while dc.readyState != 'open' or not self.provider_channels:
                await asyncio.sleep(.01)
            self.provider_channels[0].send(json.dumps({'type': 'delegation.created', 'item': {
                'id': 'd1', 'target': 'client', 'content': [{'type': 'input_text', 'text': 'Is the deploy done?'}]}}))
            while not call.pending_delegations:
                await asyncio.sleep(.01)
            await call.replace_subscription(live.time.monotonic() + 3)
            self.assertTrue(call.pending_delegations, 'fixture: the hand-off must still be running at the swap')
            self.assertEqual(len(self.provider_channels), 2, 'fixture: the swap must have happened')
            release.set()
            while call.pending_delegations:
                await asyncio.sleep(.01)
        spoken = []
        try:
            async with asyncio.timeout(1.5):
                while True:
                    spoken.append(json.dumps(await self.received.get()))
        except TimeoutError:
            pass
        self.assertTrue(any('The deploy log shows a clean exit.' in s for s in spoken), 'dropped after the renewal swap')

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

    async def test_gather_answer_does_not_race_the_driver(self):
        relay = self.Relay("You're on a detached HEAD at 2dd8589.", 0.1, source="gather")
        call, flags, sent, took = await self.delegate(relay, 0.6)
        self.assertEqual(flags, [True])  # FAST_LANE_OFF: the profile plugin starts no second lane
        self.assertEqual(len(sent), 1)
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

    async def test_smol_answer_does_not_preempt_the_driver(self):
        relay = self.Relay("Canberra.", 0.05)
        call, _, sent, _ = await self.delegate(relay, 0.2, answer="The capital of Australia is Canberra.")
        self.assertEqual(sent, ["The capital of Australia is Canberra."])
        self.assertEqual(call.counts['host_delegation_covered'], 0)

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


class RequestLedgerHelpers(unittest.TestCase):
    """The ledger's classifier and claim detection, with made-up requests."""

    def test_actions_and_questions(self):
        for text in ("Kick off the garden hill climb", "Can you start the sensor sweep", "please eject the blue drive",
                     "approve the first three and reject the rest", "go ahead and deploy it", "Set up a nightly watering job",
                     "and install the new driver", "Fix the greenhouse dashboard"):
            self.assertEqual(live.request_kind(text), "action", text)
        for text in ("What's running right now", "how is the sweep going", "Is the watering job running",
                     "what did the last run find", "Why did the deploy fail", "tell me about the ferns",
                     "how many lanes are running", "did it start", "Tell me what the last run found"):
            self.assertEqual(live.request_kind(text), "question", text)

    def test_proof_lines_are_split_off_the_spoken_answer(self):
        text, proofs = live.split_proof("Started the sweep.\nPROOF: kanban t_77 sweep\nproof: pid 4242")
        self.assertEqual(text, "Started the sweep.")
        self.assertEqual(proofs, [("kanban", "t_77", "sweep"), ("pid", "4242", "")])
        self.assertEqual(live.split_proof("Nothing started, the drive is read-only.")[1], [])

    def test_claim_words_and_the_caveat(self):
        for claim in ("Kicked it off.", "It's running now.", "Done, the drive is ejected.", "I started the sweep.", "Started it, no problems."):
            self.assertTrue(live.CLAIMS_WORK.search(claim), claim)
        for plain in ("I can't do that without sudo.", "The sweep needs your decision on the drive."):
            self.assertFalse(live.CLAIMS_WORK.search(plain), plain)
        self.assertEqual(live.unproven_claim("Kicked it off."), "Kicked it off. I haven't confirmed it started.")
        self.assertTrue(live.unproven_claim("x " * 100 + "started").endswith("I haven't confirmed it started."))
        for honest in ("Nothing started, the drive is read-only.", "I couldn't start it without sudo.",
                       "It has not started yet; it needs your decision."):
            self.assertFalse(live.Call.claims_work(honest), honest)
        self.assertTrue(live.Call.claims_work("Started it, no problems."))

    def test_proof_exists_checks_the_thing(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            self.assertTrue(live.proof_exists("file", f.name))
        self.assertFalse(live.proof_exists("file", "/nonexistent/z0live-proof"))
        self.assertTrue(live.proof_exists("pid", str(os.getpid())))
        self.assertFalse(live.proof_exists("pid", "999999999"))
        self.assertFalse(live.proof_exists("commit", "not-a-sha"))

    def test_the_unproven_line_is_a_status_line_and_promises_nothing(self):
        self.assertTrue(live.status_line(live.UNPROVEN))
        self.assertFalse(live.promises_new_work(live.UNPROVEN))


if __name__ == '__main__':
    unittest.main()
