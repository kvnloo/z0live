import asyncio
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    import local_voice_replay as lvr
    from local_voice_replay import conversation, grade
finally:
    sys.path[:] = original_path

# Stand-in for bench/lure_tts_helper.py: same stdout framing (<I sample count, then float32 PCM; 0 ends an utterance,
# and one 0 before the first request means ready). Amplitude encodes the request text length.
FAKE_HELPER = textwrap.dedent('''
    import json, select, struct, sys, time
    out = sys.stdout.buffer
    def frame(x):
        out.write(struct.pack('<I', len(x)) + struct.pack('<%df' % len(x), *x)); out.flush()
    frame([])
    for line in sys.stdin:
        req = json.loads(line)
        if 'cancel' in req:
            continue
        v = len(req['text']) / 100
        for _ in range(int(req.get('chunks', 3))):
            if select.select([sys.stdin], [], [], 0)[0]:
                if 'cancel' in json.loads(sys.stdin.readline()):
                    break
            frame([v] * 4)
            time.sleep(0.02)
        frame([])
''')


def input_pcm(*segments):
    return np.concatenate([np.full(round(48000 * duration), amplitude, '<i2')
                           for duration, amplitude in segments]).tobytes()


class VoiceReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_silence_then_speech_plays_only_after_endpoint(self):
        class Models:
            async def asr(self, pcm, marks):
                return 'What is two plus two?'

            async def reply(self, messages, marks):
                return 'Four.' if messages[-1]['content'] == 'What is two plus two?' else 'Wrong.'

            async def tts(self, text):
                if text == 'Four.':
                    yield np.full(4800, 1234, '<i2')

        audio, rows = await conversation(Models(), input_pcm((0.2, 0), (0.16, 2000), (0.1, 0)),
                                         -43, 80, drain=True)
        samples = np.frombuffer(audio, '<i2')
        self.assertEqual([r['status'] for r in rows], ['played'])
        self.assertGreaterEqual(np.flatnonzero(samples)[0], round(0.44 * 48000))
        self.assertEqual(np.count_nonzero(samples == 1234), 4800)

    async def test_barge_in_drops_unheard_pcm(self):
        class Models:
            async def asr(self, pcm, marks):
                return 'Hello.'

            async def reply(self, messages, marks):
                return 'Hello back.'

            async def tts(self, text):
                yield np.full(48000, 1234, '<i2')
                await asyncio.sleep(10)

        audio, rows = await conversation(Models(), input_pcm((0.16, 2000), (0.24, 0), (0.14, 2000)),
                                         -43, 80)
        self.assertEqual([r['status'] for r in rows], ['barge_in'])
        samples = np.frombuffer(audio, '<i2')
        self.assertTrue(np.any(samples[:round(0.4 * 48000)]))
        self.assertFalse(np.any(samples[round(0.52 * 48000):]))

    def test_skip_grades_the_audible_deferral_not_the_control_token(self):
        self.assertTrue(grade({'skip': True}, "I can't check that from here.", audible=True))
        self.assertFalse(grade({'skip': True}, 'SKIP', audible=True))
        self.assertFalse(grade({'skip': True}, 'Nothing new in your email.', audible=True))


class LureOptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = str(Path(self.tmp.name) / 'run')

    def tearDown(self):
        self.tmp.cleanup()

    def lure_argv(self, *extra):
        return ['--out', self.out, '--tts', 'lure', '--lure-model', self.tmp.name, '--lure-voice', self.tmp.name,
                '--lure-voice-text', 'Reference words.', *extra]

    def test_default_is_liquid_with_lure_defaults_inert(self):
        args = lvr.parse_args(['--out', self.out])
        self.assertEqual(args.tts, 'liquid')
        self.assertEqual(args.lure_first_chunk_frames, 1)  # fc1 default: R8-10h-r2b KEEP
        self.assertTrue(args.lure_snapshot)

    def test_lure_first_chunk_frames_defaults_to_1_and_4_still_overrides(self):
        self.assertEqual(lvr.parse_args(self.lure_argv()).lure_first_chunk_frames, 1)
        fc4 = lvr.parse_args(self.lure_argv('--lure-first-chunk-frames', '4'))
        self.assertEqual(fc4.lure_first_chunk_frames, 4)
        helper = lvr.lure_helper_argv(fc4)
        self.assertEqual(helper[helper.index('--first-chunk-frames') + 1], '4')

    def test_lure_helper_first_chunk_frames_default_is_1(self):
        import subprocess
        helper = Path(lvr.__file__).resolve().parent / 'lure_tts_helper.py'
        out = subprocess.run([sys.executable, str(helper), '--help'], capture_output=True, text=True, check=True).stdout
        self.assertIn('codec frames in the first Lure chunk (default: 1)', ' '.join(out.split()))

    def test_lure_flags_parse(self):
        args = lvr.parse_args(self.lure_argv('--lure-first-chunk-frames', '1', '--no-lure-snapshot'))
        self.assertEqual((args.tts, args.lure_first_chunk_frames, args.lure_snapshot), ('lure', 1, False))

    def test_bad_lure_options_are_rejected(self):
        for argv in (self.lure_argv('--lure-first-chunk-frames', '0'),
                     ['--out', self.out, '--tts', 'lure'],  # model, voice and its text are required for lure
                     ['--out', self.out, '--tts', 'kokoro']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                lvr.parse_args(argv)

    def test_helper_argv_carries_frames_and_snapshot(self):
        on = lvr.lure_helper_argv(lvr.parse_args(self.lure_argv('--lure-first-chunk-frames', '1')))
        self.assertEqual(on[on.index('--first-chunk-frames') + 1], '1')
        self.assertNotIn('--no-snapshot', on)
        off = lvr.lure_helper_argv(lvr.parse_args(self.lure_argv('--no-lure-snapshot')))
        self.assertEqual(off[off.index('--first-chunk-frames') + 1], '1')
        self.assertIn('--no-snapshot', off)

    def test_utterance_dump_is_off_by_default(self):
        args = lvr.parse_args(self.lure_argv())
        self.assertIsNone(args.lure_dump)
        self.assertEqual(lvr.lure_extra(args), {'tts': 'lure', 'lure_first_chunk_frames': 1, 'lure_snapshot': True})
        self.assertEqual(lvr.lure_extra(lvr.parse_args(['--out', self.out])), {})

    def test_utterance_dump_flags_parse_and_report(self):
        dump = str((Path(self.tmp.name) / 'wavs').resolve())
        args = lvr.parse_args(self.lure_argv('--lure-dump', dump, '--lure-dump-tag', 'r1_fc1'))
        self.assertEqual((str(args.lure_dump), args.lure_dump_tag), (dump, 'r1_fc1'))
        self.assertEqual(lvr.lure_extra(args)['lure_dump'], dump)

    def test_utterance_dump_needs_lure_and_a_safe_tag(self):
        dump = str(Path(self.tmp.name) / 'wavs')
        for argv in (['--out', self.out, '--lure-dump', dump],  # Liquid TTS has no per-utterance dump
                     self.lure_argv('--lure-dump', dump, '--lure-dump-tag', '../x')):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                lvr.parse_args(argv)

    def test_play_pcm_keeps_liquid_conversion_bytes(self):
        got = lvr.play_pcm(np.array([0.5, -2.0, 1.0, 0.0], '<f4'))
        self.assertEqual(got.tobytes(), np.array([16383, 16383, -32767, -32767, 32767, 32767, 0, 0], '<i2').tobytes())


class LureClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_cancel_against_fake_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'fake_helper.py'
            helper.write_text(FAKE_HELPER)
            async with lvr.LureTTS([sys.executable, '-I', str(helper)], Path(tmp) / 'lure.log') as lure:
                full = [c async for c in lure.tts('x' * 50)]
                self.assertEqual(len(full), 3)
                self.assertEqual(full[0].tobytes(), lvr.play_pcm(np.full(4, 0.5, '<f4')).tobytes())
                async with contextlib.aclosing(lure.tts('y' * 25)) as stream:  # barge-in: stop after one chunk
                    async for _ in stream:
                        break
                after = [c async for c in lure.tts('z' * 10)]  # no stale 0.25 chunks leak into the next turn
                self.assertEqual({int(v) for c in after for v in c}, {int(0.1 * 32767)})


class UtteranceDumpTest(unittest.IsolatedAsyncioTestCase):
    async def test_dump_writes_wav_and_index_per_utterance(self):
        import json
        import wave
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'fake_helper.py'
            helper.write_text(FAKE_HELPER)
            dump = lvr.UtteranceDump(Path(tmp) / 'wavs', 'r2_fc4_A')
            async with lvr.LureTTS([sys.executable, '-I', str(helper)], Path(tmp) / 'lure.log', dump) as lure:
                dump.start_call('coffee-ding')
                full = [c async for c in lure.tts('x' * 50)]
                async with contextlib.aclosing(lure.tts('y' * 25)) as stream:  # barge-in after one chunk
                    async for _ in stream:
                        break
                dump.start_call('ai-release')
                [c async for c in lure.tts('z' * 10)]
            self.assertEqual(len(full), 3)
            rows = [json.loads(x) for x in (Path(tmp) / 'wavs' / 'utterances.jsonl').read_text().splitlines()]
            self.assertEqual([(r['call'], r['turn'], r['tag'], r['complete']) for r in rows],
                             [('coffee-ding', 0, 'r2_fc4_A', True), ('coffee-ding', 1, 'r2_fc4_A', False),
                              ('ai-release', 0, 'r2_fc4_A', True)])
            self.assertEqual(rows[0]['id'], 'r2_fc4_A__coffee-ding__t00')
            self.assertEqual(rows[0]['text'], 'x' * 50)
            with wave.open(str(Path(tmp) / 'wavs' / (rows[0]['id'] + '.wav'))) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                                 (1, 2, 24000, 12))
                self.assertEqual(set(np.frombuffer(w.readframes(12), '<i2').tolist()), {16383})
            self.assertEqual(rows[0]['samples'], 12)
            self.assertLess(rows[1]['samples'], 12)

    async def test_no_dump_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'fake_helper.py'
            helper.write_text(FAKE_HELPER)
            async with lvr.LureTTS([sys.executable, '-I', str(helper)], Path(tmp) / 'lure.log') as lure:
                self.assertEqual(len([c async for c in lure.tts('x' * 50)]), 3)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['fake_helper.py', 'lure.log'])
