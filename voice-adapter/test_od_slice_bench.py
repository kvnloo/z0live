"""Contract for bench/od_slice_bench.py (ChainedEngine on_device slice with real local stages).

Fake stages and a fake clock only: no model, GPU or subprocess beyond a tiny stand-in STT helper.
"""
import asyncio
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

    def test_normalize_words(self):
        self.assertEqual(osb.norm_words("What's the  capital, of France?"), ['whats', 'the', 'capital', 'of', 'france'])


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
