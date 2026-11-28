"""R8-10h: per-call interleaved Lure arms and per-turn wall-epoch/load rows for bench/local_voice_replay.py.

No model is loaded: ASR and the reply are stubs and the Lure helper is a small fake process with the real framing.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    import local_voice_replay as lvr
finally:
    sys.path[:] = original_path

# Fake bench/lure_tts_helper.py: same stdout framing; every sample is first_chunk_frames / 10, so the audio of a turn
# tells which helper (arm) spoke it. 4 chunks of 0.2 s each (0.8 s queued) so a barge-in can cut an utterance.
FAKE_HELPER = textwrap.dedent('''
    import argparse, json, select, struct, sys, time
    ap = argparse.ArgumentParser()
    ap.add_argument('--first-chunk-frames', type=int, required=True)
    a, _ = ap.parse_known_args()
    out = sys.stdout.buffer
    def frame(x):
        out.write(struct.pack('<I', len(x)) + struct.pack('<%df' % len(x), *x)); out.flush()
    frame([])
    for line in sys.stdin:
        req = json.loads(line)
        if 'cancel' in req:
            continue
        for _ in range(4):
            if select.select([sys.stdin], [], [], 0)[0]:
                if 'cancel' in json.loads(sys.stdin.readline()):
                    break
            frame([a.first_chunk_frames / 10] * 4800)
            time.sleep(0.02)
        frame([])
''')
ARMS = 'fc4_A:4,fc1:1,fc4_B:4'


def input_pcm(*segments):
    return np.concatenate([np.full(round(48000 * duration), amplitude, '<i2')
                           for duration, amplitude in segments]).tobytes()


class StubModels:
    """ASR/reply stubs around one TTS backend (as lvr.Models does for Lure)."""
    def __init__(self, lure):
        self.lure = lure

    async def asr(self, pcm, marks):
        return 'Hello there.'

    async def reply(self, messages, marks):
        return 'Hello back to you, this is a reply.'

    async def tts(self, text):
        async with contextlib.aclosing(self.lure.tts(text)) as stream:
            async for samples in stream:
                yield samples


class ArgsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = str(Path(self.tmp.name) / 'run')

    def tearDown(self):
        self.tmp.cleanup()

    def lure_argv(self, *extra):
        return ['--out', self.out, '--tts', 'lure', '--lure-model', self.tmp.name, '--lure-voice', self.tmp.name,
                '--lure-voice-text', 'Reference words.', *extra]

    def bad(self, argv):
        with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            lvr.parse_args(argv)


class DefaultUnchangedTest(ArgsBase):
    def test_default_args_have_interleave_off(self):
        for argv in (['--out', self.out], self.lure_argv()):
            args = lvr.parse_args(argv)
            self.assertIsNone(args.interleave_arms)
        self.assertEqual(lvr.lure_extra(lvr.parse_args(self.lure_argv())),
                         {'tts': 'lure', 'lure_first_chunk_frames': 4, 'lure_snapshot': True})
        self.assertEqual(lvr.lure_extra(lvr.parse_args(['--out', self.out])), {})

    def test_default_helper_argv_unchanged(self):
        args = lvr.parse_args(self.lure_argv('--lure-first-chunk-frames', '2'))
        argv = lvr.lure_helper_argv(args)
        self.assertEqual(argv[argv.index('--first-chunk-frames') + 1], '2')
        self.assertEqual(argv[-2:], ['--first-chunk-frames', '2'])


class DefaultRowsTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_conversation_rows_have_only_the_old_keys(self):
        class Models:
            async def asr(self, pcm, marks):
                return 'Two plus two?'

            async def reply(self, messages, marks):
                return 'Four.'

            async def tts(self, text):
                yield np.full(4800, 1234, '<i2')

        _, rows = await lvr.conversation(Models(), input_pcm((0.2, 0), (0.16, 2000), (0.1, 0)), -43, 80, drain=True)
        self.assertEqual([sorted(r) for r in rows], [['marks', 'reply', 'status', 'transcript']])
        self.assertEqual(sorted(rows[0]['marks']), ['asr_start', 'first_pcm', 'gemma_start', 'last_voice',
                                                    'tts_done', 'tts_start', 'voiced_playout'])

    async def test_default_replay_result_keys_and_files_unchanged(self):
        class Models:
            lure = None

            async def asr(self, pcm, marks):
                return 'Hi.'

            async def reply(self, messages, marks):
                return 'Hello.'

            async def tts(self, text):
                yield np.full(4800, 1234, '<i2')

        with tempfile.TemporaryDirectory() as tmp:
            args = lvr.parse_args(['--out', str(Path(tmp) / 'run'), '--calls', 'c1'])
            args.out.mkdir()
            pcm = input_pcm((0.2, 0), (0.16, 2000), (0.6, 0))
            with mock.patch.object(lvr.dr, 'replay_input', return_value=pcm):
                result = await lvr.replay(Models(), args)
            self.assertEqual(sorted(result), ['ai', 'calls_done', 'engine', 'errors', 'minutes', 'out', 'per_call'])
            self.assertEqual(sorted(result['per_call']['c1']), ['ai_talk_s', 'backchannels', 'minutes',
                                                               'spoke_first', 'turns'])
            self.assertEqual(sorted(p.name for p in args.out.iterdir()), ['c1-ai.wav', 'c1-human.wav', 'c1-local.json'])
            rows = json.loads((args.out / 'c1-local.json').read_text())
            self.assertEqual([sorted(r) for r in rows], [['marks', 'reply', 'status', 'transcript']])


class ArmFlagsTest(ArgsBase):
    def test_arm_spec_parses(self):
        args = lvr.parse_args(self.lure_argv('--interleave-arms', ARMS, '--interleave-rounds', '2',
                                             '--interleave-seed', '7'))
        self.assertEqual(args.interleave_arms, [('fc4_A', 4), ('fc1', 1), ('fc4_B', 4)])
        self.assertEqual((args.interleave_rounds, args.interleave_seed), (2, 7))
        extra = lvr.lure_extra(args)
        self.assertEqual(extra['interleave_arms'], {'fc4_A': 4, 'fc1': 1, 'fc4_B': 4})
        self.assertEqual((extra['interleave_rounds'], extra['interleave_seed']), (2, 7))

    def test_bad_arm_specs_are_rejected(self):
        for spec in ('fc4_A:4', 'a:4,a:1', 'a:0,b:4', 'a:33,b:4', 'a:x,b:1', '../a:4,b:1', 'a4,b:1'):
            self.bad(self.lure_argv('--interleave-arms', spec))
        self.bad(['--out', self.out, '--interleave-arms', ARMS])  # needs --tts lure
        self.bad(self.lure_argv('--interleave-arms', ARMS, '--slm'))
        self.bad(self.lure_argv('--interleave-arms', ARMS, '--interleave-rounds', '0'))
        self.bad(self.lure_argv('--interleave-rounds', '2'))  # rounds/seed only with arms

    def test_helper_argv_per_arm_carries_its_frames(self):
        args = lvr.parse_args(self.lure_argv('--interleave-arms', ARMS))
        for _, fc in args.interleave_arms:
            argv = lvr.lure_helper_argv(args, fc)
            self.assertEqual(argv[argv.index('--first-chunk-frames') + 1], str(fc))
            self.assertEqual(argv.count('--first-chunk-frames'), 1)


class ScheduleTest(unittest.TestCase):
    names = ('fc4_A', 'fc1', 'fc4_B')

    def test_orders_are_rotations_and_mirrors(self):
        orders = lvr.arm_orders(self.names)
        self.assertEqual(len(orders), 6)
        self.assertEqual(len(set(orders)), 6)
        self.assertEqual(orders[0], self.names)
        for o in orders:
            self.assertEqual(sorted(o), sorted(self.names))

    def test_schedule_is_balanced_per_block(self):
        for units in (6, 12, 24):
            sched = lvr.arm_schedule(self.names, units, seed=3)
            self.assertEqual(len(sched), units)
            for pos in range(3):
                counts = {n: sum(o[pos] == n for o in sched) for n in self.names}
                self.assertEqual(set(counts.values()), {units // 3}, (units, pos, counts))
            for start in range(0, units, 6):  # every complete block uses each order exactly once
                self.assertEqual(len(set(sched[start:start + 6])), 6)

    def test_schedule_is_deterministic_from_the_seed(self):
        self.assertEqual(lvr.arm_schedule(self.names, 12, seed=5), lvr.arm_schedule(self.names, 12, seed=5))
        self.assertTrue(any(lvr.arm_schedule(self.names, 12, seed=s) != lvr.arm_schedule(self.names, 12, seed=0)
                            for s in range(1, 6)))

    def test_two_and_four_arms(self):
        self.assertEqual(lvr.arm_orders(('a', 'b')), [('a', 'b'), ('b', 'a')])
        orders = lvr.arm_orders(('a', 'b', 'c', 'd'))
        self.assertEqual(len(orders), 8)
        for pos in range(4):
            self.assertEqual({sum(o[pos] == n for o in orders) for n in 'abcd'}, {2})


class TurnLogTest(unittest.TestCase):
    def test_turn_log_records_epochs_load_and_cpu(self):
        log = lvr.TurnLog(clock=lambda: 1000.0, load=lambda: (2.5, 1.0, 0.5), cpu=iter([10.0, 10.5]).__next__)
        start = time.monotonic()
        log.begin(start, now=lambda: 1000.0)
        row = {'marks': {'last_voice': start + 1.0, 'asr_start': start + 1.25}}
        log.eos(row, start + 1.25)
        row['marks']['voiced_playout'] = start + 2.0
        log.finish(row)
        self.assertEqual(row['wall'], {'call_start': 1000.0, 'last_voice': 1001.0, 'asr_start': 1001.25,
                                       'voiced_playout': 1002.0})
        self.assertEqual(row['load1'], 2.5)
        self.assertEqual(row['cpu_pct'], 40.0)  # 0.5 s CPU over 1.25 s wall since call start


class InterleavedReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_rows_and_no_state_leak_between_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'fake_helper.py'
            helper.write_text(FAKE_HELPER)
            out = Path(tmp) / 'run'
            args = lvr.parse_args(['--out', str(out), '--calls', 'c1,c2', '--tts', 'lure', '--lure-model', tmp,
                                   '--lure-voice', tmp, '--lure-voice-text', 'Words.', '--silence-ms', '80',
                                   '--interleave-arms', ARMS, '--interleave-rounds', '1', '--interleave-seed', '1'])
            out.mkdir()
            # speech, endpoint -> 0.8 s reply, the next speech barges in and cuts it, then that turn plays out.
            pcm = input_pcm((0.3, 0), (0.16, 2000), (0.24, 0), (0.14, 2000), (1.8, 0))
            expected = {name: int(np.float32(fc / 10) * 32767) for name, fc in args.interleave_arms}
            async with contextlib.AsyncExitStack() as stack:
                pipes = {}
                for name, fc in args.interleave_arms:
                    argv = [sys.executable, '-I', str(helper)] + lvr.lure_helper_argv(args, fc)[3:]
                    lure = await stack.enter_async_context(lvr.LureTTS(argv, Path(tmp) / f'{name}.log'))
                    pipes[name] = StubModels(lure)
                t0 = time.time()
                with mock.patch.object(lvr.dr, 'replay_input', return_value=pcm):
                    result = await lvr.replay_interleaved(pipes, args)
                t1 = time.time()
            self.assertEqual(result['errors'], [])
            sched = result['schedule']
            self.assertEqual([(u['round'], u['call']) for u in sched], [(1, 'c1'), (1, 'c2')])
            self.assertEqual([tuple(u['order']) for u in sched], lvr.arm_schedule(('fc4_A', 'fc1', 'fc4_B'), 2, 1))
            self.assertEqual(sorted(result['per_arm']), ['fc1', 'fc4_A', 'fc4_B'])
            self.assertEqual(result['calls_done'], 6)
            seen_slots = []
            for unit in sched:
                for slot, arm in enumerate(unit['order']):
                    d = out / f"r{unit['round']}" / arm
                    rows = json.loads((d / f"{unit['call']}-local.json").read_text())
                    self.assertEqual([r['status'] for r in rows], ['barge_in', 'played'])
                    for row in rows:
                        self.assertEqual((row['arm'], row['fc'], row['round'], row['call'], row['slot']),
                                         (arm, dict(args.interleave_arms)[arm], unit['round'], unit['call'], slot))
                        self.assertTrue(t0 <= row['wall']['call_start'] <= row['wall']['last_voice']
                                        <= row['wall']['asr_start'] <= row['wall']['voiced_playout'] <= t1, row)
                        self.assertIsInstance(row['load1'], float)
                        self.assertGreaterEqual(row['cpu_pct'], 0.0)
                    import wave
                    with wave.open(str(d / f"{unit['call']}-ai.wav")) as w:
                        ai = np.frombuffer(w.readframes(w.getnframes()), '<i2')
                    self.assertEqual(set(np.unique(ai[ai != 0]).tolist()), {expected[arm]}, (unit, arm))
                    seen_slots.append(arm)
            self.assertEqual(len(seen_slots), 6)
            key = f"r1/{sched[0]['order'][0]}/c1"
            self.assertIn(key, result['per_call'])
            self.assertEqual(result['per_call'][key]['arm'], sched[0]['order'][0])


if __name__ == '__main__':
    unittest.main()
