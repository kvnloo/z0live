"""Contract for bench/od_slice_bench.py (ChainedEngine on_device slice with real local stages).

Fake stages and a fake clock only: no model, GPU or subprocess beyond a tiny stand-in STT helper.
"""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    import od_slice_bench as osb
finally:
    sys.path[:] = original_path


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def fake_stages(clock, stt_ms=100, model_first_ms=50, token_ms=10, tts_first_ms=40):
    async def stt(pcm):
        clock.now += stt_ms * 1_000_000
        await asyncio.sleep(0)
        return 'what is the capital of france'

    async def model(text):
        clock.now += model_first_ms * 1_000_000
        for i, tok in enumerate(['Paris ', 'is ', 'the capital. ', 'It is ', 'big.']):
            if i in (1, 2):  # later tokens run concurrently with TTS; keep the fake clock to the first clause
                clock.now += token_ms * 1_000_000
            await asyncio.sleep(0)
            yield tok

    async def tts(clause):
        clock.now += tts_first_ms * 1_000_000
        await asyncio.sleep(0)
        yield b'\x01\x00' * 480
        yield b'\x01\x00' * 480

    return stt, model, tts


class TurnMetrics(unittest.TestCase):
    def test_stage_breakdown_sums_to_eos_first_audio(self):
        clock = FakeClock()
        row = asyncio.run(osb.run_turn(fake_stages(clock), b'\x00\x00' * 320, turn_id=3, clock=clock))
        self.assertTrue(row['ok'])
        self.assertEqual(row['eos_to_stt_ms'], 100)
        self.assertEqual(row['stt_to_first_token_ms'], 50)
        self.assertEqual(row['first_token_to_first_clause_ms'], 20)  # 'Paris ' 'is ' 'the capital. ' -> 2 gaps
        self.assertEqual(row['first_clause_to_first_audio_ms'], 40)
        self.assertEqual(row['eos_to_first_audio_ms'], 210)
        parts = sum(row[k] for k in osb.STAGES)
        self.assertAlmostEqual(parts, row['eos_to_first_audio_ms'])
        self.assertEqual(row['first_clause'], 'Paris is the capital.')
        self.assertEqual(row['transcript'], 'what is the capital of france')
        self.assertEqual(row['turn_id'], 3)

    def test_failed_stage_is_not_ok_and_has_no_latency(self):
        clock = FakeClock()
        stt, model, tts = fake_stages(clock)

        async def bad_tts(clause):
            raise RuntimeError('boom')
            yield b''

        row = asyncio.run(osb.run_turn((stt, model, bad_tts), b'\x00\x00', clock=clock))
        self.assertFalse(row['ok'])
        self.assertEqual(row['error_stage'], 'tts')
        self.assertIsNone(row['eos_to_first_audio_ms'])

    def test_pcm_bytes_adapter_skips_empty_numpy_chunks(self):
        async def numpy_tts(clause):
            yield np.zeros(0, '<i2')
            yield np.ones(4, '<i2')

        async def collect():
            return [c async for c in osb.as_bytes(numpy_tts)('hi')]

        self.assertEqual(asyncio.run(collect()), [np.ones(4, '<i2').tobytes()])


def tone_clip(lead_s=0.3, speech_s=1.0, tail_s=0.2, amp=3000):
    """Silence, a 1 kHz tone (well above -43 dBFS), then a short quiet tail: speech ends at lead_s + speech_s."""
    t = np.arange(int(speech_s * osb.RATE)) / osb.RATE
    tone = (amp * np.sin(2 * np.pi * 1000 * t + np.pi / 2)).astype('<i2')  # cos: last sample is not a zero crossing
    return np.concatenate([np.zeros(int(lead_s * osb.RATE), '<i2'), tone,
                           np.zeros(int(tail_s * osb.RATE), '<i2')]).tobytes()


class FakeSleep:
    def __init__(self, clock):
        self.clock = clock

    async def __call__(self, seconds):
        self.clock.now += round(seconds * 1e9)


class Endpointing(unittest.TestCase):
    def test_eos_is_declared_one_silence_window_after_speech_end(self):
        clip = tone_clip()
        for silence_ms in (500, 300):
            end_s, eos_s, utt = osb.endpoint_offline(clip, silence_ms=silence_ms)
            self.assertAlmostEqual(end_s, 1.3, delta=1 / osb.RATE)
            self.assertGreaterEqual(eos_s - end_s, silence_ms / 1000 - 1e-6)
            self.assertLessEqual(eos_s - end_s, silence_ms / 1000 + 0.02)  # one 20 ms frame of quantization
            self.assertGreaterEqual(len(utt) // 2, osb.RATE)  # the whole 1 s of speech reaches STT

    def test_speech_running_to_clip_end_still_endpoints_on_appended_silence(self):
        end_s, eos_s, _ = osb.endpoint_offline(tone_clip(tail_s=0), silence_ms=500)
        self.assertAlmostEqual(end_s, 1.3, delta=1 / osb.RATE)
        self.assertAlmostEqual(eos_s - end_s, 0.5, delta=0.02)

    def test_live_turn_adds_endpointer_delay_to_eos_first_audio(self):
        clock = FakeClock()
        row = asyncio.run(osb.run_live_turn(fake_stages(clock), tone_clip(), turn_id=1, silence_ms=500,
                                            clock=clock, sleep=FakeSleep(clock)))
        self.assertTrue(row['ok'])
        self.assertAlmostEqual(row['endpointer_ms'], 500, delta=20)
        self.assertEqual(row['eos_to_first_audio_ms'], 210)
        self.assertAlmostEqual(row['speech_end_to_first_audio_ms'], row['endpointer_ms'] + 210)
        self.assertAlmostEqual(row['endpointer_declared_ms'], row['endpointer_ms'])

    def test_silent_clip_is_an_endpointer_error(self):
        clock = FakeClock()
        row = asyncio.run(osb.run_live_turn(fake_stages(clock), b'\x00\x00' * 16000, clock=clock,
                                            sleep=FakeSleep(clock)))
        self.assertFalse(row['ok'])
        self.assertEqual(row['error_stage'], 'endpointer')
        self.assertIsNone(row['speech_end_to_first_audio_ms'])

    def test_pause_longer_than_window_is_a_split_error_not_a_negative_latency(self):
        clip = tone_clip(tail_s=0.7) + tone_clip(lead_s=0, tail_s=0)  # 0.7 s pause mid-utterance
        clock = FakeClock()
        row = asyncio.run(osb.run_live_turn(fake_stages(clock), clip, silence_ms=500, clock=clock,
                                            sleep=FakeSleep(clock)))
        self.assertFalse(row['ok'])
        self.assertEqual(row['error'], 'split before speech end')
        self.assertLess(row['endpointer_declared_ms'], 0)
        self.assertIsNone(row['speech_end_to_first_audio_ms'])

    def test_zero_window_is_the_offline_eos_of_od_slice_1(self):
        clock = FakeClock()
        row = asyncio.run(osb.run_live_turn(fake_stages(clock), tone_clip(), silence_ms=0, clock=clock))
        self.assertEqual(row['eos_to_first_audio_ms'], 210)
        self.assertIsNone(row['endpointer_ms'])

    def test_summary_bars_the_speech_end_metric_when_present(self):
        rows = [{'arm': arm, 'ok': True, 'endpointer_ms': 500, 'eos_to_first_audio_ms': 300,
                 'speech_end_to_first_audio_ms': 800 + i, 'eos_to_stt_ms': 100, 'stt_to_first_token_ms': 30,
                 'first_token_to_first_clause_ms': 100, 'first_clause_to_first_audio_ms': 70}
                for arm in ('A', 'A2') for i in range(5)]
        s = osb.summarize(rows)
        self.assertEqual(s['primary'], 'speech_end_to_first_audio_ms')
        self.assertEqual(s['dominant_stage'], 'endpointer_ms')
        self.assertFalse(s['bar']['p50_le_500'])
        self.assertTrue(s['bar']['p95_le_1000'])
        self.assertAlmostEqual(s['aa_band_ms']['p50'], 0)


class Schedule(unittest.TestCase):
    def test_aa_arms_interleave_and_cover_every_utterance(self):
        plan = osb.schedule(runs=3, n_utts=12, seed=7)
        self.assertEqual([p['arm'] for p in plan], ['A', 'A2', 'A2', 'A', 'A', 'A2'])
        self.assertEqual([p['run'] for p in plan], [1, 1, 2, 2, 3, 3])
        for p in plan:
            self.assertEqual(sorted(p['order']), list(range(12)))
        self.assertEqual(plan, osb.schedule(runs=3, n_utts=12, seed=7))  # seeded, reproducible
        self.assertNotEqual(plan[0]['order'], plan[1]['order'])

    def test_window_sweep_labels_repeats_as_aa_and_interleaves_per_utterance(self):
        arms = osb.window_arms('500,300,200,500')
        self.assertEqual(arms, [('W500', 500), ('W300', 300), ('W200', 200), ('W500b', 500)])
        plan = osb.schedule_windows(runs=2, n_utts=12, seed=1, arms=arms)
        self.assertEqual([p['run'] for p in plan], [1, 2])
        for p in plan:
            self.assertEqual(len(p['turns']), 48)
            for k in range(12):  # each utterance played once per arm back to back
                block = p['turns'][4 * k:4 * k + 4]
                self.assertEqual(len({t[0] for t in block}), 1)
                self.assertEqual(sorted(t[1] for t in block), ['W200', 'W300', 'W500', 'W500b'])
        firsts = [plan[0]['turns'][4 * k][1] for k in range(4)]
        self.assertEqual(firsts, ['W500', 'W300', 'W200', 'W500b'])  # arm order rotates per utterance
        # same seeded utterance order as the A/A plan's first pass
        self.assertEqual([plan[0]['turns'][4 * k][0] for k in range(12)], osb.schedule(1, 12, 1)[0]['order'])

    def test_window_flag_defaults_off_and_validates(self):
        base = ['--out', '/tmp/x', '--utterances', '/tmp/u', '--whisper-model', '/tmp/w', '--gguf', '/tmp/g',
                '--lure-model', 'm', '--lure-voice', 'v', '--lure-voice-text', 't']
        a = osb.parse_args(base)
        self.assertEqual((a.arm_windows, a.endpointer_silence_ms), ('', 500))
        self.assertEqual(osb.parse_args(base + ['--arm-windows', '500,300,200,500']).arm_windows, '500,300,200,500')
        for bad in ('250', '40', 'x'):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                osb.parse_args(base + ['--arm-windows', bad])


class Summary(unittest.TestCase):
    def test_percentiles_bar_and_aa_band(self):
        rows = []
        for arm, base in (('A', 400), ('A2', 410)):
            for i in range(10):
                rows.append({'arm': arm, 'ok': True, 'eos_to_first_audio_ms': base + i * 10,
                             'eos_to_stt_ms': 100, 'stt_to_first_token_ms': 50 + i,
                             'first_token_to_first_clause_ms': 200, 'first_clause_to_first_audio_ms': 50,
                             'stt_exact': i % 2 == 0})
        rows.append({'arm': 'A', 'ok': False, 'eos_to_first_audio_ms': None})
        s = osb.summarize(rows)
        self.assertEqual(s['arms']['A']['n'], 10)
        self.assertEqual(s['arms']['A']['errors'], 1)
        self.assertAlmostEqual(s['arms']['A']['eos_to_first_audio_ms']['p50'], 445)
        self.assertAlmostEqual(s['arms']['A2']['eos_to_first_audio_ms']['p50'], 455)
        self.assertAlmostEqual(s['aa_band_ms']['p50'], 10)
        self.assertEqual(s['pooled']['n'], 20)
        self.assertEqual(s['dominant_stage'], 'first_token_to_first_clause_ms')
        self.assertTrue(s['bar']['p50_le_500'])
        self.assertTrue(s['bar']['p95_le_1000'])
        self.assertAlmostEqual(s['pooled']['stt_exact_rate'], 0.5)

    def test_sweep_aa_band_and_premature_endpoint_count(self):
        rows = [{'arm': arm, 'ok': True, 'speech_end_to_first_audio_ms': base + i, 'endpointer_ms': 100}
                for arm, base in (('W500', 700), ('W500b', 720), ('W200', 400)) for i in range(5)]
        rows.append({'arm': 'W200', 'ok': False, 'error': 'split before speech end'})
        s = osb.summarize(rows)
        self.assertAlmostEqual(s['aa_band_ms']['p50'], 20)
        self.assertEqual(s['arms']['W200']['premature_endpoints'], 1)
        self.assertEqual(s['arms']['W500']['premature_endpoints'], 0)

    def test_normalize_words(self):
        self.assertEqual(osb.norm_words("What's the  capital, of France?"), ['whats', 'the', 'capital', 'of', 'france'])


class _VirtualSelector:
    """Selector wrapper: a select(timeout) advances virtual time instead of blocking (no real I/O in these tests)."""
    def __init__(self, loop, selector):
        self._loop, self._selector = loop, selector

    def select(self, timeout=None):
        if timeout:
            self._loop.vt += timeout
        return self._selector.select(0)

    def __getattr__(self, name):
        return getattr(self._selector, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    """Deterministic virtual time: asyncio.sleep in stages and the frame pacer share one clock, concurrency intact."""
    def __init__(self):
        super().__init__()
        self.vt = 0.0
        self._selector = _VirtualSelector(self, self._selector)

    def time(self):
        return self.vt


def vrun(make):
    """Run make(clock) on a VirtualLoop; clock is perf_counter_ns-shaped (integer ns of virtual time)."""
    loop = VirtualLoop()
    try:
        return loop.run_until_complete(make(lambda: round(loop.vt * 1e9)))
    finally:
        loop.close()


def sleep_stages(stt_ms=100, model_first_ms=50, tts_first_ms=40, calls=None):
    """Fake stages with fixed asyncio sleeps (virtual time): EOS -> first audio = stt + model first + tts first."""
    calls = calls if calls is not None else []

    async def stt(pcm):
        calls.append(len(pcm))
        await asyncio.sleep(stt_ms / 1000)
        return 'what is the capital of france'

    async def model(text):
        await asyncio.sleep(model_first_ms / 1000)
        yield 'Paris is the capital. '
        await asyncio.sleep(0.05)
        yield 'It is big.'

    async def tts(clause):
        await asyncio.sleep(tts_first_ms / 1000)
        yield b'\x01\x00' * 480
        await asyncio.sleep(0.02)
        yield b'\x01\x00' * 480

    return stt, model, tts


def paused_clip(pause_s=0.3):
    """1 s of speech, a mid-utterance pause, 0.5 s more speech, a short tail: speech ends at 0.3 + 1 + pause + 0.5."""
    return tone_clip(tail_s=pause_s) + tone_clip(lead_s=0, speech_s=0.5, tail_s=0.2)


class SpeculativeStart(unittest.TestCase):
    """OD-slice-3 contract. Speculative arm: STT + model start on the partial utterance after spec_ms of trailing
    silence; the 500 ms endpointer still decides the turn (commit). Playback is gated on the commit, so no audio from
    a speculative turn is ever audible before the commit; speech_end -> first audio = max(commit, spec ready)."""

    def spec(self, pcm, stages, spec_ms=150, silence_ms=500, plays=None):
        async def go(clock):
            return await osb.run_spec_turn(stages, pcm, turn_id=7, silence_ms=silence_ms, spec_ms=spec_ms,
                                           clock=clock, sleep=asyncio.sleep,
                                           on_play=None if plays is None else (lambda ns, n: plays.append((ns, n))))
        return vrun(go)

    def test_spec_starts_at_150_ms_and_audio_waits_for_the_500_ms_commit(self):
        plays, calls = [], []
        row = self.spec(tone_clip(), sleep_stages(calls=calls), plays=plays)  # spec ready ~140 + 190 < commit 500
        self.assertTrue(row['ok'], row)
        self.assertEqual((row['spec_started'], row['spec_cancelled'], row['spec_committed']), (1, 0, 1))
        self.assertAlmostEqual(row['spec_start_ms'], 140, delta=20)  # 150 ms -> 7 frames (Endpointer floor rule)
        self.assertAlmostEqual(row['endpointer_ms'], 500, delta=20)  # commit = the unchanged 500 ms endpointer
        self.assertAlmostEqual(row['first_audio_ready_ms'], row['spec_start_ms'] + 190, delta=1)
        self.assertLess(row['first_audio_ready_ms'], row['endpointer_ms'])
        commit_ns = row['commit_ns']
        self.assertTrue(plays and all(ns >= commit_ns for ns, _ in plays))  # nothing audible before the commit
        self.assertAlmostEqual(row['speech_end_to_first_audio_ms'], row['endpointer_ms'], delta=1)  # = max(...)
        self.assertAlmostEqual(row['gate_hold_ms'], row['endpointer_ms'] - row['first_audio_ready_ms'], delta=1)
        self.assertAlmostEqual(row['eos_to_first_audio_ms'], 0, delta=1)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0] // 2, osb.RATE)  # the whole 1 s of speech reached STT
        self.assertEqual((row['wasted_stt_ms'], row['wasted_model_ms'], row['wasted_tts_ms']), (0, 0, 0))
        self.assertEqual(row['transcript'], 'what is the capital of france')

    def test_slow_stages_set_first_audio_when_spec_is_ready_after_the_commit(self):
        plays = []
        row = self.spec(tone_clip(), sleep_stages(stt_ms=400), plays=plays)  # ready ~140 + 490 > 500
        self.assertTrue(row['ok'], row)
        self.assertGreater(row['first_audio_ready_ms'], row['endpointer_ms'])
        self.assertAlmostEqual(row['speech_end_to_first_audio_ms'], row['first_audio_ready_ms'], delta=1)
        self.assertEqual(row['gate_hold_ms'], 0)
        self.assertAlmostEqual(row['speech_end_to_first_audio_ms'],
                               row['endpointer_ms'] + row['eos_to_first_audio_ms'], delta=1e-6)

    def test_resumed_speech_cancels_the_spec_turn_counts_waste_and_the_utterance_continues(self):
        plays, calls = [], []
        row = self.spec(paused_clip(0.3), sleep_stages(calls=calls), plays=plays)
        self.assertTrue(row['ok'], row)
        self.assertEqual((row['spec_started'], row['spec_cancelled'], row['spec_committed']), (2, 1, 1))
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[1], calls[0] + osb.RATE // 2)  # the committed STT got both speech segments
        self.assertAlmostEqual(row['wasted_stt_ms'], 100, delta=1)  # STT finished inside the 300 ms pause
        # model cut at the end of the first resumed voiced frame: pause 300 + frame 20 - spec start 140 - STT 100
        self.assertAlmostEqual(row['wasted_model_ms'], 300 + 20 - 140 - 100, delta=1)
        self.assertAlmostEqual(row['endpointer_ms'], 500, delta=20)  # measured from the true (final) speech end
        self.assertTrue(all(ns >= row['commit_ns'] for ns, _ in plays))

    def test_cancelled_spec_audio_is_never_played(self):
        plays = []
        fast = sleep_stages(stt_ms=10, model_first_ms=10, tts_first_ms=10)  # spec audio ready inside the pause
        row = self.spec(paused_clip(0.4), fast, plays=plays)
        self.assertTrue(row['ok'], row)
        self.assertEqual(row['spec_cancelled'], 1)
        self.assertGreater(row['wasted_tts_ms'], 0)
        self.assertEqual(len(plays), row['chunks'])  # only the committed turn's chunks were played
        self.assertTrue(all(ns >= row['commit_ns'] for ns, _ in plays))

    def test_split_before_speech_end_is_decided_by_the_commit_exactly_as_w500(self):
        clip = tone_clip(tail_s=0.7) + tone_clip(lead_s=0, tail_s=0)  # 0.7 s pause >= the 500 ms commit window
        spec = self.spec(clip, sleep_stages())
        self.assertFalse(spec['ok'])
        self.assertEqual(spec['error'], 'split before speech end')
        self.assertIsNone(spec['speech_end_to_first_audio_ms'])
        clock = FakeClock()
        w500 = asyncio.run(osb.run_live_turn(fake_stages(clock), clip, silence_ms=500, clock=clock,
                                             sleep=FakeSleep(clock)))
        self.assertEqual(w500['error'], spec['error'])
        self.assertAlmostEqual(w500['endpointer_declared_ms'], spec['endpointer_declared_ms'], delta=1)

    def test_spec_window_must_be_shorter_than_the_commit_window(self):
        with self.assertRaises(ValueError):
            self.spec(tone_clip(), sleep_stages(), spec_ms=500)


class ArmModes(unittest.TestCase):
    BASE = ['--out', '/tmp/x', '--utterances', '/tmp/u', '--whisper-model', '/tmp/w', '--gguf', '/tmp/g',
            '--lure-model', 'm', '--lure-voice', 'v', '--lure-voice-text', 't']

    def test_modes_parse_with_explicit_and_auto_aa_labels(self):
        self.assertEqual(osb.mode_arms('w500,spec150,w500b', 500),
                         [('W500', 500, None), ('SPEC150', 500, 150), ('W500b', 500, None)])
        self.assertEqual(osb.mode_arms('w500,w500', 500), [('W500', 500, None), ('W500b', 500, None)])
        for bad in ('x500', 'spec500', 'spec0', 'w250', 'spec145'):
            with self.assertRaises(ValueError):
                osb.mode_arms(bad, 500)

    def test_w500_control_turn_is_identical_to_od_slice_2b_w500(self):
        rows = []
        for fn in (lambda st, clk: osb.run_live_turn(st, tone_clip(), turn_id=4, silence_ms=500, clock=clk,
                                                     sleep=FakeSleep(clk)),
                   lambda st, clk: osb.run_arm_turn(st, tone_clip(), turn_id=4, silence_ms=500, spec_ms=None,
                                                    clock=clk, sleep=FakeSleep(clk))):
            clock = FakeClock()
            rows.append(asyncio.run(fn(fake_stages(clock), clock)))
        self.assertEqual(rows[0], rows[1])
        self.assertFalse(any(k.startswith('spec_') or k.startswith('wasted_') for k in rows[1]))

    def test_mode_schedule_matches_the_window_sweep_shape(self):
        arms = osb.mode_arms('w500,spec150,w500b', 500)
        plan = osb.schedule_windows(runs=3, n_utts=12, seed=1, arms=arms)
        self.assertEqual(sum(len(p['turns']) for p in plan), 108)  # n 36 per arm
        for p in plan:
            for k in range(12):
                block = p['turns'][3 * k:3 * k + 3]
                self.assertEqual(len({t[0] for t in block}), 1)
                self.assertEqual(sorted(t[1] for t in block), ['SPEC150', 'W500', 'W500b'])
                self.assertEqual({t[2] for t in block}, {500})  # the commit window never differs between arms
        self.assertEqual([plan[0]['turns'][3 * k][0] for k in range(12)], osb.schedule(1, 12, 1)[0]['order'])

    def test_arm_modes_flag_defaults_off_validates_and_excludes_arm_windows(self):
        a = osb.parse_args(self.BASE)
        self.assertEqual((a.arm_modes, a.arm_windows), ('', ''))
        self.assertEqual(osb.parse_args(self.BASE + ['--arm-modes', 'w500,spec150,w500b']).arm_modes,
                         'w500,spec150,w500b')
        for extra in (['--arm-modes', 'spec700'], ['--arm-modes', 'w500', '--arm-windows', '500']):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                osb.parse_args(self.BASE + extra)

    def test_fake_stage_dry_run_needs_no_model_paths(self):
        a = osb.parse_args(['--out', '/tmp/x', '--utterances', '/tmp/u', '--fake-stages', '115,62,90'])
        self.assertEqual(a.fake_stages, (115, 62, 90))


class SpecSummary(unittest.TestCase):
    def test_report_fields_per_arm(self):
        rows = [{'arm': 'W500', 'ok': True, 'speech_end_to_first_audio_ms': 700 + i, 'endpointer_ms': 480}
                for i in range(5)]
        rows += [{'arm': 'SPEC150', 'ok': True, 'speech_end_to_first_audio_ms': 480 + i, 'endpointer_ms': 480,
                  'spec_started': 1 + (i == 0), 'spec_cancelled': int(i == 0), 'spec_committed': 1,
                  'wasted_stt_ms': 100.0 * (i == 0), 'wasted_model_ms': 60.0 * (i == 0), 'wasted_tts_ms': 0.0}
                 for i in range(5)]
        rows.append({'arm': 'SPEC150', 'ok': False, 'error': 'split before speech end', 'spec_started': 1,
                     'spec_cancelled': 0, 'spec_committed': 1, 'wasted_stt_ms': 0, 'wasted_model_ms': 0,
                     'wasted_tts_ms': 0})
        s = osb.summarize(rows)['arms']
        sp = s['SPEC150']
        self.assertEqual(sp['speech_end_to_first_audio_ms']['p50'], 482)
        self.assertEqual(sp['endpointer_ms']['p50'], 480)
        self.assertEqual((sp['false_cuts'], sp['premature_endpoints']), (1, 1))
        self.assertEqual((sp['spec_started'], sp['spec_cancelled'], sp['spec_committed']), (7, 1, 6))
        self.assertEqual((sp['wasted_stt_ms'], sp['wasted_model_ms'], sp['wasted_tts_ms']), (100, 60, 0))
        self.assertEqual(s['W500']['false_cuts'], 0)
        self.assertNotIn('spec_started', s['W500'])


GOLDEN = Path(__file__).with_name('fixtures') / 'od_slice3_spec150_rows.json'
GOLDEN_CASES = {  # name -> (clip, stage kwargs); frozen at OD-slice-3 (1d0e0e5) by bench/make_spec150_golden.py
    'plain': (lambda: tone_clip(), {}),
    'slow_stt': (lambda: tone_clip(), {'stt_ms': 400}),
    'resumed': (lambda: paused_clip(0.3), {}),
    'resumed_fast': (lambda: paused_clip(0.4), {'stt_ms': 10, 'model_first_ms': 10, 'tts_first_ms': 10}),
    'split': (lambda: tone_clip(tail_s=0.7) + tone_clip(lead_s=0, tail_s=0), {}),
}


def golden_spec_row(name, **kw):
    """One SPEC150 arm turn (run_arm_turn, spec_ms=150) for a GOLDEN_CASES entry, as canonical JSON-able data."""
    clip, stage_kw = GOLDEN_CASES[name]

    async def go(clock):
        return await osb.run_arm_turn(sleep_stages(**stage_kw), clip(), turn_id=7, silence_ms=500, spec_ms=150,
                                      clock=clock, sleep=asyncio.sleep, **kw)
    return json.loads(json.dumps(vrun(go)))


def text_stages(text, stt_ms=100, **kw):
    """sleep_stages whose STT returns `text` (Whisper-like punctuation) after stt_ms of virtual time."""
    calls = kw.pop('calls', [])
    _, model, tts = sleep_stages(stt_ms=stt_ms, calls=calls, **kw)

    async def stt(pcm):
        calls.append(len(pcm))
        await asyncio.sleep(stt_ms / 1000)
        return text
    return stt, model, tts


class EarlyCommit(unittest.TestCase):
    """OD-slice-4 contract. SPEC150EC = SPEC150 plus a commit at the earliest moment the CURRENT speculative turn's
    transcript is known, >= 200 ms of trailing silence was counted and rule ec1 passes; else the 500 ms commit."""

    def ec(self, pcm, stages, plays=None):
        async def go(clock):
            return await osb.run_spec_turn(stages, pcm, turn_id=7, silence_ms=500, spec_ms=150, clock=clock,
                                           sleep=asyncio.sleep, early_commit=True,
                                           on_play=None if plays is None else (lambda ns, n: plays.append((ns, n))))
        return vrun(go)

    def test_rule_ec1(self):
        yes = ('What is the capital of France?', ' Who wrote Romeo and Juliet? ', 'How many days are in a week.',
               'Tell me now!', 'What do bees make?')
        no = ('What is the capital of France', 'Hi there?', 'Yes.', '', None, 'I went to the store and.',
              'Tell me about the.', 'What do you think of?', 'Is it this or?', 'I was going to, um.',
              'So because?', 'Ask him to.', 'It is a.', 'Why is that so?', 'Wait...uh?', 'Ends with comma,')
        for t in yes:
            self.assertTrue(osb.confident_turn_end(t), t)
        for t in no:
            self.assertFalse(osb.confident_turn_end(t), t)
        self.assertEqual(osb.EC_CONTINUATION, frozenset(
            'and but so or because um uh the a to of'.split()))  # the preregistered list, verbatim
        self.assertEqual((osb.EC_SILENCE_MS, osb.EC_RULE), (200, 'ec1'))

    def test_fast_stt_still_waits_for_200_ms_of_silence(self):
        plays = []
        row = self.ec(tone_clip(), text_stages('What is the capital of France?', stt_ms=10), plays=plays)
        self.assertTrue(row['ok'], row)
        self.assertEqual(row['early_commit'], 1)
        self.assertGreaterEqual(row['ec_commit_quiet_frames'], 10)
        self.assertGreaterEqual(row['endpointer_ms'], 200)  # transcript ready at ~150 ms; the commit waits
        self.assertLess(row['endpointer_ms'], 240)
        self.assertTrue(plays and all(ns >= row['commit_ns'] for ns, _ in plays))

    def test_early_commit_never_fires_before_200_ms_for_any_stt_latency(self):
        for stt_ms in (0, 1, 5, 30, 55, 59, 61, 95):
            row = self.ec(tone_clip(), text_stages('What is the capital of France?', stt_ms=stt_ms))
            self.assertEqual(row['early_commit'], 1, stt_ms)
            self.assertGreaterEqual(row['endpointer_ms'], 200 - 1e-6, stt_ms)

    def test_commit_is_the_moment_the_stt_result_lands_not_the_next_frame(self):
        row = self.ec(tone_clip(), text_stages('What is the capital of France?', stt_ms=105))
        self.assertEqual(row['early_commit'], 1)
        self.assertAlmostEqual(row['endpointer_ms'], 140 + 105, delta=1)  # spec start + STT, mid-frame
        self.assertAlmostEqual(row['speech_end_to_first_audio_ms'], row['first_audio_ready_ms'], delta=1)
        self.assertLess(row['speech_end_to_first_audio_ms'], 500)

    def test_rule_fail_falls_back_to_the_500_ms_commit_like_spec150(self):
        row = self.ec(tone_clip(), text_stages('what is the capital of france'))  # no end punctuation
        self.assertTrue(row['ok'], row)
        self.assertEqual((row['early_commit'], row['ec_rule_evals'], row['ec_last_pass']), (0, 1, False))
        self.assertAlmostEqual(row['endpointer_ms'], 500, delta=20)
        spec = golden_spec_row('plain')
        for k in ('endpointer_ms', 'speech_end_to_first_audio_ms', 'first_audio_ready_ms', 'commit_ns'):
            self.assertAlmostEqual(row[k], spec[k], delta=1e-6)

    def test_resumed_speech_after_an_early_commit_is_a_false_cut_and_the_turn_still_plays(self):
        plays = []
        row = self.ec(paused_clip(0.4), text_stages('What is the capital?', stt_ms=60), plays=plays)
        self.assertFalse(row['ok'])
        self.assertEqual(row['error'], 'split before speech end')
        self.assertEqual((row['early_commit'], row['ec_resumed']), (1, True))
        self.assertLess(row['endpointer_declared_ms'], 0)
        self.assertEqual(row['transcript'], 'What is the capital?')
        self.assertTrue(plays)  # committed and played, as a real system would
        self.assertEqual(row['spec_cancelled'], 0)
        s = osb.summarize([dict(row, arm='SPEC150EC')])['arms']['SPEC150EC']
        self.assertEqual((s['false_cuts'], s['ec_false_cuts'], s['early_commits'], s['ec_fire_rate']), (1, 1, 1, 1))
        # the control arm on the same clip and stages is not cut (the 400 ms pause < the 500 ms commit)
        ctl = vrun(lambda clock: osb.run_spec_turn(
            text_stages('What is the capital?', stt_ms=60), paused_clip(0.4), turn_id=7, silence_ms=500,
            spec_ms=150, clock=clock, sleep=asyncio.sleep))
        self.assertTrue(ctl['ok'], ctl)

    def test_a_cancelled_spec_turns_transcript_never_commits(self):
        calls = []
        row = self.ec(paused_clip(0.2), text_stages('What is the capital?', stt_ms=100, calls=calls))
        self.assertTrue(row['ok'], row)  # spec 1 (140 ms into the pause) is cancelled at 220 ms, before STT lands
        self.assertEqual((row['spec_started'], row['spec_cancelled']), (2, 1))
        self.assertEqual((row['early_commit'], row['ec_rule_evals']), (1, 1))  # only the current turn was judged
        self.assertGreaterEqual(row['endpointer_ms'], 200)
        self.assertGreater(calls[1], calls[0] + osb.RATE // 2)

    def test_spec150_rows_are_byte_identical_to_od_slice_3(self):
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(golden), sorted(GOLDEN_CASES))
        for name in GOLDEN_CASES:
            for kw in ({}, {'ec': None}):
                self.assertEqual(json.dumps(golden_spec_row(name, **kw), sort_keys=True),
                                 json.dumps(golden[name], sort_keys=True), name)
            self.assertFalse(any(k.startswith('ec_') or k == 'early_commit' for k in golden_spec_row(name)))

    def test_modes_parse_ec_arm_and_schedule(self):
        arms = osb.mode_arms('spec150,spec150ec,spec150b', 500)
        self.assertEqual(arms, [('SPEC150', 500, 150), ('SPEC150EC', 500, 150, 'ec'), ('SPEC150b', 500, 150)])
        for bad in ('w500ec', 'spec150ecb', 'spec200ec', 'spec150ec,spec150ec'):
            with self.assertRaises(ValueError):
                osb.mode_arms(bad, 500)
        plan = osb.schedule_windows(runs=3, n_utts=12, seed=1, arms=arms)
        self.assertEqual(sum(len(p['turns']) for p in plan), 108)
        self.assertEqual([plan[0]['turns'][3 * k][0] for k in range(12)], osb.schedule(1, 12, 1)[0]['order'])
        firsts = [plan[0]['turns'][3 * k][1] for k in range(3)]
        self.assertEqual(firsts, ['SPEC150', 'SPEC150EC', 'SPEC150b'])  # rotation as OD-slice-3
        a = osb.parse_args(ArmModes.BASE + ['--arm-modes', 'spec150,spec150ec,spec150b'])
        self.assertEqual(a.arm_modes, 'spec150,spec150ec,spec150b')

    def test_aa_band_uses_spec150_pair(self):
        rows = [{'arm': arm, 'ok': True, 'speech_end_to_first_audio_ms': base + i, 'endpointer_ms': 480}
                for arm, base in (('SPEC150', 480), ('SPEC150b', 483), ('SPEC150EC', 300)) for i in range(5)]
        self.assertAlmostEqual(osb.summarize(rows)['aa_band_ms']['p50'], 3)


SLOW_STT_HELPER = textwrap.dedent('''
    import json, struct, sys, time
    out, inp = sys.stdout, sys.stdin.buffer
    print(json.dumps({"event": "ready"}), flush=True)
    while True:
        head = inp.read(4)
        if len(head) < 4:
            break
        n, = struct.unpack('<I', head)
        inp.read(2 * n)
        time.sleep(0.3)
        print(json.dumps({"text": " samples %d " % n, "ms": 1.0}), flush=True)
''')


class HelperCancel(unittest.TestCase):
    def test_cancelled_request_reply_is_discarded_not_read_as_the_next_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'helper.py'
            helper.write_text(SLOW_STT_HELPER)

            async def go():
                async with osb.HelperSTT([sys.executable, '-I', str(helper)], Path(tmp) / 'stt.log') as stt:
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(stt.stt(b'\x00\x00' * 320), 0.05)  # spec turn cancelled mid-STT
                    return await stt.stt(b'\x00\x00' * 5)

            self.assertEqual(asyncio.run(go()), 'samples 5')


FAKE_STT_HELPER = textwrap.dedent('''
    import json, struct, sys
    out, inp = sys.stdout, sys.stdin.buffer
    print(json.dumps({"event": "ready"}), flush=True)
    while True:
        head = inp.read(4)
        if len(head) < 4:
            break
        n, = struct.unpack('<I', head)
        inp.read(2 * n)
        print(json.dumps({"text": " samples %d " % n, "ms": 1.0}), flush=True)
''')


class WhisperClient(unittest.TestCase):
    def test_framed_pcm_round_trip_through_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'helper.py'
            helper.write_text(FAKE_STT_HELPER)
            log = Path(tmp) / 'stt.log'

            async def go():
                async with osb.HelperSTT([sys.executable, '-I', str(helper)], log) as stt:
                    return [await stt.stt(b'\x00\x00' * 320), await stt.stt(b'\x00\x00' * 5)]

            self.assertEqual(asyncio.run(go()), ['samples 320', 'samples 5'])


if __name__ == '__main__':
    unittest.main()
