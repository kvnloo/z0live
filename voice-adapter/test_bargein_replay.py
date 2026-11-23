"""Tests for the frozen barge-in replay harness (bench/bargein_replay.py, bargein_replay/1)."""

from array import array
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


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def run_cli(*args):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'out.json'
        proc = subprocess.run(
            [sys.executable, '-B', str(BENCH_DIR / 'bargein_replay.py'), *args, '--output', str(out)],
            capture_output=True, text=True, timeout=900)
        data = out.read_bytes() if out.exists() else b''
    return proc, data


class ArmCTest(unittest.TestCase):
    """Arm C: local-VAD timing plus a render-referenced echo gate and a minimum-speech gate."""

    # Names the gate must never touch: labels, echo-model internals, network legs, phone speaker.
    FORBIDDEN = {'trial', 'kind', 'echo_ms', 'echo_gain', 'onset', 'speaker', 'up', 'ws_up',
                 'ws_back', 'down', 'phone_extra', 'missed_valid', 'false_barge_in', 'mic', 'stimuli',
                 'residual', 'echo_model', 'double_talk', 'capture', 'residual_echo'}

    @classmethod
    def setUpClass(cls):
        cls.bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        cls.trials = list(replay.stimuli(20, 39))

    def gate_cfg(self, **overrides):
        return cfg(**({'gate_echo_rho': 0.9, 'gate_min_speech_ms': 40, 'gate_window_ms': 20,
                       'gate_lag_max_ms': 250} | overrides))

    def test_flag_parses(self):
        proc, data = run_cli('--seed', '39', '--trials', '10', '--arm-c',
                             '--gate-echo-rho', '0.8', '--gate-min-speech-ms', '30')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(sorted(summary['arms']), ['A', 'B', 'C'])
        self.assertEqual(summary['parameters']['gate_echo_rho'], 0.8)
        self.assertEqual(summary['parameters']['gate_min_speech_ms'], 30)
        bad, _ = run_cli('--trials', '10', '--arm-c', '--gate-min-speech-ms', '25')
        self.assertNotEqual(bad.returncode, 0)

    def test_ab_unchanged_by_arm_c(self):
        _, plain = run_cli('--seed', '39', '--trials', '20')
        _, with_c = run_cli('--seed', '39', '--trials', '20', '--arm-c')
        plain, with_c = json.loads(plain), json.loads(with_c)
        for arm in ('A', 'B'):
            self.assertEqual(sha256(canonical(with_c['arms'][arm])).hexdigest(),
                             sha256(canonical(plain['arms'][arm])).hexdigest())
        self.assertNotIn('C', plain['arms'])
        self.assertFalse(any(key.startswith('gate_') for key in plain['parameters']))

    def test_gate_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.FlushGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self.FORBIDDEN, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith('gate_'), node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, self.FORBIDDEN, node.value)
        self.assertEqual(list(inspect.signature(replay.FlushGate.observe).parameters),
                         ['self', 't', 'packet'])
        self.assertEqual(list(inspect.signature(replay.FlushGate.render).parameters),
                         ['self', 't', 'frame'])

    def test_gate_off_min20_matches_b(self):
        loose = self.gate_cfg(gate_echo_rho=0, gate_min_speech_ms=20)
        for trial in self.trials:
            b = replay.replay(trial, 'B', loose, self.bot)
            c = replay.replay(trial, 'C', loose, self.bot)
            self.assertEqual(b['latency_ms'], c['latency_ms'])
            self.assertEqual(b['trace'].get('adapter_flush_ms'), c['trace'].get('adapter_flush_ms'))

    def test_echo_gate_vetoes_render_copy(self):
        echo = next(t for t in self.trials if t['kind'] == 'echo')
        self.assertTrue(replay.replay(echo, 'B', self.gate_cfg(), self.bot)['false_barge_in'])
        self.assertFalse(replay.replay(echo, 'C', self.gate_cfg(), self.bot)['false_barge_in'])

    def test_arm_c_deterministic(self):
        proc, data = run_cli('--seed', '39', '--trials', '20', '--arm-c', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        _, again = run_cli('--seed', '39', '--trials', '20', '--arm-c')
        self.assertEqual(sha256(data).hexdigest(), sha256(again).hexdigest())


ARM_C_SEED40_SHA256 = '03bf7ab8269744f76c2d177d900ced730626266424c26ed444e0ee882ae78b2c'
BASE_KEYS = ('id', 'kind', 'onset', 'mic', 'echo_ms', 'up', 'ws_up', 'ws_back', 'down', 'phone_extra')


class DefaultBytesFrozenTest(unittest.TestCase):
    """Without --echo-model residual, arm C's seed-40 holdout bytes stay exactly as published."""

    def test_arm_c_seed40_bytes_unchanged(self):
        proc, data = run_cli('--seed', '40', '--trials', '200', '--arm-c',
                             '--gate-echo-rho', '0.7', '--gate-min-speech-ms', '40')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), ARM_C_SEED40_SHA256)
        self.assertNotIn('harness', json.loads(data))

    def test_explicit_copy_model_is_default(self):
        _, plain = run_cli('--seed', '39', '--trials', '20', '--arm-c')
        _, copy = run_cli('--seed', '39', '--trials', '20', '--arm-c', '--echo-model', 'copy')
        self.assertEqual(plain, copy)
        self.assertNotIn('echo_model', json.loads(plain)['parameters'])


class ResidualEchoTest(unittest.TestCase):
    """bargein_replay/2: opt-in residual echo (RIR, bulk delay, ERL, soft clip, noise, double-talk)."""

    @classmethod
    def setUpClass(cls):
        cls.bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        cls.copy = list(replay.stimuli(200, 39))
        cls.residual = list(replay.stimuli(200, 39, echo_model='residual'))

    def gate_cfg(self, **overrides):
        return cfg(**({'gate_echo_rho': 0.9, 'gate_min_speech_ms': 40, 'gate_window_ms': 20,
                       'gate_lag_max_ms': 250, 'echo_model': 'residual'} | overrides))

    def test_harness_ids(self):
        self.assertEqual(replay.HARNESS, 'bargein_replay/1')
        self.assertEqual(replay.HARNESS_RESIDUAL, 'bargein_replay/2')

    def test_base_generator_stream_unchanged(self):
        for plain, res in zip(self.copy, self.residual):
            for key in BASE_KEYS:
                self.assertEqual(plain[key], res[key], key)
            self.assertNotIn('residual', plain)
            self.assertIn('residual', res)

    def test_residual_parameters_in_spec(self):
        dt = 0
        for trial in self.residual:
            r = trial['residual']
            self.assertTrue(20 <= r['delay_ms'] <= 120)
            self.assertTrue(10 <= r['rir_ms'] <= 60)
            self.assertEqual(r['taps'][0], (0, 1.0))
            self.assertEqual(len(r['taps']), 13)
            for delay, gain in r['taps'][1:]:
                self.assertTrue(16 <= delay <= r['rir_ms'] * 16)
                self.assertTrue(0 < abs(gain) <= 0.6)
            self.assertTrue(6 <= r['erl_db'] <= 20)
            self.assertTrue(12000 <= r['clip_level'] <= 20000)
            self.assertTrue(40 <= r['noise_rms'] <= 160)
            self.assertEqual(len(r['noise']), replay.BOT_MS * replay.SAMPLES_MS)
            if trial['kind'] == 'valid':
                dt += r['double_talk']
                if r['double_talk']:
                    self.assertTrue(85 <= r['double_talk_ms'] <= 200)
            else:
                self.assertFalse(r['double_talk'])
        self.assertTrue(50 <= dt <= 90, dt)  # about half of 140 valid trials

    def test_residual_echo_is_not_an_exact_copy(self):
        echoes = [t for t in self.residual if t['kind'] == 'echo'][:6]
        rhos = [replay.replay(t, 'C', self.gate_cfg(), self.bot)['trace']['gate_max_rho'] for t in echoes]
        self.assertTrue(all(rho < 0.99 for rho in rhos), rhos)
        copy_echo = next(t for t in self.copy if t['kind'] == 'echo')
        copy_rho = replay.replay(copy_echo, 'C', self.gate_cfg(echo_model='copy'), self.bot)['trace']['gate_max_rho']
        self.assertGreater(copy_rho, 0.999)

    def test_double_talk_mixes_echo_under_speech(self):
        trial = next(t for t in self.residual if t['kind'] == 'valid' and t['residual']['double_talk'])
        speaker = array('h', [9000, -9000] * (replay.BOT_MS * replay.SAMPLES_MS))
        start = trial['onset'] * replay.SAMPLES_MS
        packet = replay.capture(trial, speaker, start, 160, self.gate_cfg())
        clip = trial['mic'][:160]
        self.assertNotEqual(list(packet), list(clip))
        self.assertTrue(all(-32768 <= v <= 32767 for v in packet))
        quiet = replay.capture(trial, array('h', [0]) * len(speaker), start, 160, self.gate_cfg())
        noise = trial['residual']['noise'][start:start + 160]
        self.assertEqual(list(quiet), [max(-32768, min(32767, c + n)) for c, n in zip(clip, noise)])

    def test_cli_residual_marks_harness_v2(self):
        proc, data = run_cli('--seed', '39', '--trials', '20', '--arm-c', '--echo-model', 'residual', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(data)
        self.assertEqual(out['harness'], 'bargein_replay/2')
        self.assertEqual(out['parameters']['echo_model'], 'residual')
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        _, plain = run_cli('--seed', '39', '--trials', '20', '--arm-c')
        self.assertNotEqual(sha256(plain).hexdigest(), sha256(data).hexdigest())


class RecordedBotRenderTest(unittest.TestCase):
    """bargein_replay/3: opt-in --bot-render recorded (LibriSpeech bot, disjoint from the user clip)."""

    @classmethod
    def setUpClass(cls):
        cls.residual = list(replay.stimuli(60, 39, echo_model='residual'))
        cls.recorded = list(replay.stimuli(60, 39, echo_model='residual', bot_render='recorded'))
        cls.clips = {p.name: replay.clip(p) for p in sorted(replay.ASSETS.glob('*.wav'))}

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_RECORDED, 'bargein_replay/3')

    def test_base_and_residual_streams_unchanged(self):
        for res, rec in zip(self.residual, self.recorded):
            for key in BASE_KEYS:
                self.assertEqual(res[key], rec[key], key)
            self.assertEqual(res['residual'], rec['residual'])
            self.assertNotIn('bot', res)
            self.assertEqual(len(rec['bot']), replay.BOT_MS * replay.SAMPLES_MS)

    def test_bot_segment_disjoint_from_user_clip(self):
        valid = [t for t in self.recorded if t['kind'] == 'valid']
        self.assertTrue(valid)
        for trial in self.recorded:
            source = self.clips[trial['bot_clip']]
            if trial['kind'] == 'valid':
                self.assertEqual(self.clips[trial['mic_clip']], trial['mic'])
                self.assertNotEqual(trial['bot_clip'], trial['mic_clip'])
            else:
                self.assertIsNone(trial['mic_clip'])
            # every bot sample is a sample of the bot clip at the recorded rotation
            n, off = len(source), trial['bot_offset']
            self.assertTrue(0 <= off < n)
            self.assertEqual(list(trial['bot'][:2000]), [source[(off + i) % n] for i in range(2000)])
        names = list(self.clips)
        with self.assertRaises(AssertionError):
            replay.recorded_bot(39, 0, [self.clips[names[0]]], names[:1], exclude=names[0])

    def test_recorded_differs_from_periodic(self):
        periodic = replay.voice(replay.BOT_MS, 142, 11000, 115)
        self.assertNotEqual(list(self.recorded[0]['bot']), list(periodic))

    def test_recorded_deterministic_per_seed(self):
        again = list(replay.stimuli(60, 39, echo_model='residual', bot_render='recorded'))
        self.assertEqual([(t['bot_clip'], t['bot_offset'], t['bot']) for t in again],
                         [(t['bot_clip'], t['bot_offset'], t['bot']) for t in self.recorded])
        other = list(replay.stimuli(60, 38, echo_model='residual', bot_render='recorded'))
        self.assertNotEqual([t['bot_offset'] for t in other], [t['bot_offset'] for t in self.recorded])

    def test_cli_recorded_marks_v3_and_reports_rho(self):
        args = ('--seed', '39', '--trials', '20', '--arm-c', '--echo-model', 'residual')
        proc, data = run_cli(*args, '--bot-render', 'recorded', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out, summary = json.loads(data), json.loads(proc.stdout)
        self.assertEqual(out['harness'], 'bargein_replay/3')
        self.assertEqual(out['parameters']['bot_render'], 'recorded')
        self.assertIs(summary['aa_identical'], True)
        rho = summary['arms']['C']['echo_trial_max_rho']
        self.assertEqual(sorted(rho), ['n', 'p10', 'p50', 'p90'])
        self.assertIn('valid_trials_with_veto', summary['arms']['C'])
        self.assertIn('double_talk_latency_ms', summary['arms']['A'])
        self.assertIn('bot_render', out['arms']['C']['trials'][0])
        _, again = run_cli(*args, '--bot-render', 'recorded')
        self.assertEqual(sha256(data).hexdigest(), sha256(again).hexdigest())
        _, v2 = run_cli(*args)
        _, v2_explicit = run_cli(*args, '--bot-render', 'periodic')
        self.assertEqual(v2, v2_explicit)
        self.assertEqual(json.loads(v2)['harness'], 'bargein_replay/2')
        self.assertNotIn('bot_render', json.loads(v2)['parameters'])
        self.assertNotIn('echo_trial_max_rho', json.loads(v2)['arms']['C'])


class ArmDEnergyGateTest(unittest.TestCase):
    """bargein_replay/4: opt-in arm D, Geigel-style energy-ratio echo gate plus minimum speech."""

    ARGS = ('--seed', '39', '--trials', '20', '--arm-c', '--echo-model', 'residual', '--bot-render', 'recorded')

    @classmethod
    def setUpClass(cls):
        cls.bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        cls.trials = list(replay.stimuli(40, 39, echo_model='residual', bot_render='recorded'))

    def energy_cfg(self, **overrides):
        return cfg(**({'echo_model': 'residual', 'bot_render': 'recorded', 'energy_k_db': -6.0,
                       'energy_min_speech_ms': 40, 'energy_lag_max_ms': 250} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_ENERGY, 'bargein_replay/4')

    def test_gate_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.EnergyGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith('energy_'), node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, ArmCTest.FORBIDDEN, node.value)
        self.assertEqual(list(inspect.signature(replay.EnergyGate.observe).parameters), ['self', 't', 'packet'])
        self.assertEqual(list(inspect.signature(replay.EnergyGate.render).parameters), ['self', 't', 'frame'])

    def test_energy_rule_unit(self):
        gate = replay.EnergyGate(self.energy_cfg(energy_min_speech_ms=20), 400)
        loud = array('h', [8000, -8000] * (replay.FRAME_MS * replay.SAMPLES_MS // 2))
        gate.render(100, loud)
        # near-end at -12 dB re render (RMS 2000 vs 8000), hot, inside the lag window: explained -> veto
        echo = array('h', [2000, -2000] * (replay.CAPTURE_MS * replay.SAMPLES_MS // 2))
        self.assertFalse(gate.observe(200, echo))
        self.assertFalse(gate.observe(210, echo))
        self.assertEqual(gate.vetoes, 2)
        # near-end at 0 dB re render: unexplained -> counts as speech; flush after 20 ms
        speech = array('h', [8000, -8000] * (replay.CAPTURE_MS * replay.SAMPLES_MS // 2))
        self.assertFalse(gate.observe(220, speech))
        self.assertTrue(gate.observe(230, speech))
        # outside the lag window the render does not explain anything
        late = replay.EnergyGate(self.energy_cfg(energy_min_speech_ms=10), 800)
        late.render(100, loud)
        self.assertTrue(late.observe(500, echo))
        self.assertEqual(late.vetoes, 0)

    def test_gate_off_min20_matches_b(self):
        # k so low nothing is explained, M 20 -> identical to arm B's local VAD timing
        loose = self.energy_cfg(energy_k_db=-200.0, energy_min_speech_ms=20)
        for trial in self.trials:
            b = replay.replay(trial, 'B', loose, self.bot)
            d = replay.replay(trial, 'D', loose, self.bot)
            self.assertEqual(b['latency_ms'], d['latency_ms'])
            self.assertEqual(b['trace'].get('adapter_flush_ms'), d['trace'].get('adapter_flush_ms'))
            self.assertEqual(d['trace']['energy_vetoes'], 0)

    def test_cli_arm_d_marks_v4_and_leaves_abc_unchanged(self):
        proc, without = run_cli(*self.ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, with_d = run_cli(*self.ARGS, '--arm-d', '--energy-k-db', '-9', '--energy-min-speech-ms', '30', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plain, out = json.loads(without), json.loads(with_d)
        self.assertEqual(plain['harness'], 'bargein_replay/3')
        self.assertEqual(out['harness'], 'bargein_replay/4')
        self.assertEqual(sorted(out['arms']), ['A', 'B', 'C', 'D'])
        for arm in ('A', 'B', 'C'):
            self.assertEqual(canonical(out['arms'][arm]), canonical(plain['arms'][arm]), arm)
        self.assertEqual(out['parameters']['energy_k_db'], -9.0)
        self.assertEqual(out['parameters']['energy_min_speech_ms'], 30)
        self.assertEqual(out['parameters']['energy_lag_max_ms'], 250)
        self.assertNotIn('arm_d', out['parameters'])
        self.assertFalse(any(k.startswith('energy_') for k in plain['parameters']))
        summary = json.loads(proc.stdout)
        self.assertIs(summary['aa_identical'], True)
        d = summary['arms']['D']
        self.assertIn('valid_trials_with_veto', d)
        self.assertIn('double_talk_latency_ms', d)
        self.assertEqual(sorted(d['echo_trial_max_ratio_db']), ['n', 'p10', 'p50', 'p90'])
        self.assertIn('energy_vetoes', out['arms']['D']['trials'][0]['trace'])
        _, again = run_cli(*self.ARGS, '--arm-d', '--energy-k-db', '-9', '--energy-min-speech-ms', '30')
        self.assertEqual(sha256(with_d).hexdigest(), sha256(again).hexdigest())
        bad, _ = run_cli('--trials', '10', '--arm-d', '--energy-min-speech-ms', '25')
        self.assertNotEqual(bad.returncode, 0)

    def test_default_bytes_unchanged_without_arm_d(self):
        # explicit: seed-39 default and arm-C seed-40 shas are asserted in FrozenSeed39Test and
        # DefaultBytesFrozenTest; here, energy flags without --arm-d must not change any byte.
        _, plain = run_cli('--seed', '39', '--trials', '20', '--arm-c')
        _, flags = run_cli('--seed', '39', '--trials', '20', '--arm-c', '--energy-k-db', '-9')
        self.assertEqual(plain, flags)
        self.assertNotIn('harness', json.loads(plain))

    def test_sweep_default_and_arm_d(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = [sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '20',
                    '--echo-model', 'residual', '--bot-render', 'recorded']
            subprocess.run(base + ['--out', tmp + '/d', '--rhos', '0.9', '--arm-d', '--energy-k-db', '-9',
                                   '--energy-min-speech-ms', '30'], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'd', 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/4')
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D'])
            self.assertIn('valid_trials_with_veto', res['arms']['D'])
            self.assertIn('valid_trials_with_veto', res['arms']['C0.9'])
            self.assertNotIn('valid_trials_with_veto', res['arms']['A'])


if __name__ == '__main__':
    unittest.main()
