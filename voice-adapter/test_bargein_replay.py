"""Tests for the frozen barge-in replay harness (bench/bargein_replay.py, bargein_replay/1)."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

BENCH_DIR = Path(__file__).resolve().parent / 'bench'
original_path = sys.path[:]
sys.path.insert(0, str(BENCH_DIR))
try:
    import bargein_replay as replay
finally:
    sys.path[:] = original_path


# Frozen reference: seed 39, 200 paired trials per arm, default parameters.
REFERENCE_SHA256 = '001fdb0b963c24773aa05ea639ce19127c6d1d38b3cf84adc6b34ee88009cfec'
REFERENCE_PARAMETERS = dict(audio_transit_ms=12, control_ms=45, depth=4, echo_gain=0.65,
                            local_vad_ms=20, phone_ms=35, server_vad_ms=50, transport='websocket')
REFERENCE_ARMS = {
    'A': dict(false_barge_ins=29, latency_ms={'p50': 186.46875, 'p95': 219.0}, missed_valid=0,
              negative_by_kind={'backchannel': {'false': 10, 'n': 20},
                                'echo': {'false': 19, 'n': 20},
                                'noise': {'false': 0, 'n': 20}},
              negative_n=60, residual_ms={'p50': 140.5, 'p95': 167}, valid_n=140,
              valid_over_150_or_missed=139),
    'B': dict(false_barge_ins=45, latency_ms={'p50': 99.0, 'p95': 121.0}, missed_valid=0,
              negative_by_kind={'backchannel': {'false': 20, 'n': 20},
                                'echo': {'false': 20, 'n': 20},
                                'noise': {'false': 5, 'n': 20}},
              negative_n=60, residual_ms={'p50': 77.0, 'p95': 90}, valid_n=140,
              valid_over_150_or_missed=0),
}


def cfg(**overrides):
    base = dict(depth=4, phone_ms=35, control_ms=45, server_vad_ms=50,
                local_vad_ms=20, transport='websocket', echo_gain=.65, audio_transit_ms=12)
    return SimpleNamespace(**(base | overrides))


class BargeInReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        cls.trial = next(replay.stimuli(10, 39))

    def test_harness_version(self):
        self.assertEqual(replay.HARNESS, 'bargein_replay/1')

    def test_fixtures_present(self):
        wavs = sorted(p.name for p in replay.ASSETS.glob('*.wav'))
        self.assertEqual(wavs, ['2277-149896-0005.wav', '2277-149896-0026.wav'])
        self.assertTrue((replay.ASSETS / 'LibriSpeech-LICENSE.txt').is_file())
        self.assertTrue((replay.ASSETS / 'README.md').is_file())

    def test_local_flush_fences_immediately(self):
        a = replay.replay(self.trial, 'A', cfg(), self.bot)
        b = replay.replay(self.trial, 'B', cfg(), self.bot)
        slower = replay.replay(self.trial, 'B', cfg(control_ms=140), self.bot)
        trace = b['trace']
        self.assertEqual(trace['adapter_flush_ms'], trace['local_vad_ms'])
        self.assertEqual(a['trace']['adapter_flush_ms'], a['trace']['provider_speech_started_rx_ms'])
        self.assertLess(trace['last_adapter_frame_ms'], trace['adapter_flush_ms'])
        self.assertGreater(trace['phone_last_audible_end_ms'], trace['adapter_flush_ms'])
        self.assertLess(b['latency_ms'], a['latency_ms'])
        self.assertEqual(b['latency_ms'], slower['latency_ms'])
        self.assertGreater(slower['late_dropped_frames'], b['late_dropped_frames'])
        self.assertNotIn('clear_send_ms', trace)

    def test_provider_buffer_controls(self):
        row = replay.replay(self.trial, 'B', cfg(transport='provider-buffer', control_ms=80), self.bot)
        trace = row['trace']
        self.assertEqual(trace['clear_apply_ms'], trace['clear_send_ms'] + self.trial['ws_up'] + 80)
        self.assertEqual(trace['cancel_apply_ms'], trace['clear_apply_ms'])
        self.assertEqual(trace['provider_history_end_ms'], trace['truncate_audio_end_ms'])
        provider = replay.FakeProvider(cfg())
        provider.control('clear', None)
        self.assertFalse(provider.output_open)
        self.assertTrue(provider.generating)
        provider.control('cancel', None)
        self.assertFalse(provider.generating)

    def test_zero_depth_and_missed_detection(self):
        row = replay.replay(self.trial, 'B', cfg(depth=0, local_vad_ms=3000), self.bot)
        self.assertTrue(row['missed_valid'])
        self.assertEqual(row['trace']['last_adapter_frame_ms'], replay.BOT_MS - replay.FRAME_MS + 12)


class FrozenSeed39Test(unittest.TestCase):
    """The CLI with --seed 39 --trials 200 --aa must reproduce the frozen summary exactly."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'seed39.json'
            proc = subprocess.run(
                [sys.executable, '-B', str(BENCH_DIR / 'bargein_replay.py'),
                 '--seed', '39', '--trials', '200', '--aa', '--output', str(out)],
                capture_output=True, text=True, timeout=600)
            cls.proc = proc
            cls.data = out.read_bytes() if out.exists() else b''
        cls.summary = json.loads(proc.stdout) if proc.returncode == 0 else {}

    def test_cli_succeeds_with_aa_identical(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIs(self.summary['aa_identical'], True)

    def test_summary_matches_reference(self):
        self.assertEqual(self.summary['seed'], 39)
        self.assertEqual(self.summary['trials_per_arm'], 200)
        self.assertEqual(self.summary['parameters'], REFERENCE_PARAMETERS)
        self.assertEqual(self.summary['arms'], REFERENCE_ARMS)

    def test_output_bytes_match_reference_hash(self):
        self.assertEqual(sha256(self.data).hexdigest(), REFERENCE_SHA256)
        self.assertEqual(self.summary['sha256'], REFERENCE_SHA256)


if __name__ == '__main__':
    unittest.main()
