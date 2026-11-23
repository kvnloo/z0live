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


# A-bargein-F arm-D output (bargein_replay/4), pinned from exp/bargein-energy-gate @0ef2824.
ARM_D_F_ARGS = ('--seed', '39', '--trials', '20', '--arm-c', '--echo-model', 'residual', '--bot-render', 'recorded',
                '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30')
ARM_D_F_SHA256 = 'e32ff57ea07cfe2ecc54cc3a345429debccb01b9fb3847beec13bfffe211514e'
STRESS = ('--caller-level-db-range=-10,0', '--erl-db-range=0,6')  # '=': argparse 3.9-3.12 reject a separate '-10,0'


class StressFlagsTest(unittest.TestCase):
    """bargein_replay/5: opt-in caller level randomisation and ERL range override."""

    @classmethod
    def setUpClass(cls):
        cls.plain = list(replay.stimuli(60, 39, echo_model='residual', bot_render='recorded'))
        cls.stress = list(replay.stimuli(60, 39, echo_model='residual', bot_render='recorded',
                                         caller_level_db_range=(-10.0, 0.0), erl_db_range=(0.0, 6.0)))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_STRESS, 'bargein_replay/5')

    def test_arm_d_f_bytes_unchanged(self):
        proc, data = run_cli(*ARM_D_F_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), ARM_D_F_SHA256)

    def test_other_streams_unchanged(self):
        for p, s in zip(self.plain, self.stress):
            for key in BASE_KEYS:
                if key != 'mic':
                    self.assertEqual(p[key], s[key], key)
            for key in ('bot_clip', 'bot_offset', 'bot', 'mic_clip'):
                self.assertEqual(p[key], s[key], key)
            pr, sr = p['residual'], s['residual']
            for key in ('delay_ms', 'rir_ms', 'taps', 'clip_level', 'noise_rms', 'noise', 'double_talk',
                        'double_talk_ms'):
                self.assertEqual(pr[key], sr[key], key)
            self.assertTrue(0 <= sr['erl_db'] <= 6)
            self.assertTrue(6 <= pr['erl_db'] <= 20)

    def test_caller_gain_applied_to_valid_only(self):
        gains = []
        for p, s in zip(self.plain, self.stress):
            if p['kind'] == 'valid':
                g = s['caller_gain_db']
                gains.append(g)
                self.assertTrue(-10 <= g <= 0)
                scale = 10 ** (g / 20)
                self.assertEqual(list(s['mic']), [max(-32768, min(32767, round(v * scale))) for v in p['mic']])
            else:
                self.assertEqual(p['mic'], s['mic'])
                self.assertNotIn('caller_gain_db', s)
        self.assertGreater(max(gains) - min(gains), 3)
        self.assertNotIn('caller_gain_db', self.plain[0])

    def test_each_flag_alone(self):
        only_erl = list(replay.stimuli(20, 39, echo_model='residual', bot_render='recorded', erl_db_range=(0.0, 6.0)))
        for p, s in zip(self.plain, only_erl):
            self.assertEqual(p['mic'], s['mic'])
        only_caller = list(replay.stimuli(20, 39, echo_model='residual', bot_render='recorded',
                                          caller_level_db_range=(-10.0, 0.0)))
        for p, s in zip(self.plain, only_caller):
            self.assertEqual(p['residual']['erl_db'], s['residual']['erl_db'])

    def test_cli_marks_v5_and_is_deterministic(self):
        proc, data = run_cli(*ARM_D_F_ARGS, *STRESS, '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(data)
        self.assertEqual(out['harness'], 'bargein_replay/5')
        self.assertEqual(out['parameters']['caller_level_db_range'], [-10.0, 0.0])
        self.assertEqual(out['parameters']['erl_db_range'], [0.0, 6.0])
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        valid = [t for t in out['arms']['D']['trials'] if t['kind'] == 'valid']
        self.assertTrue(all('caller_gain_db' in t for t in valid))
        self.assertTrue(all(0 <= t['echo_path']['erl_db'] <= 6 for t in out['arms']['A']['trials']))
        _, again = run_cli(*ARM_D_F_ARGS, *STRESS)
        self.assertEqual(sha256(data).hexdigest(), sha256(again).hexdigest())
        _, plain = run_cli(*ARM_D_F_ARGS)
        self.assertNotIn('caller_level_db_range', json.loads(plain)['parameters'])
        self.assertNotIn('erl_db_range', json.loads(plain)['parameters'])

    def test_cli_rejects_bad_ranges(self):
        for args in (('--caller-level-db-range=0,-10',), ('--caller-level-db-range=x',),
                     ('--erl-db-range', '0,6'),  # needs --echo-model residual
                     ('--echo-model', 'residual', '--erl-db-range', '-1,6')):
            bad, _ = run_cli('--trials', '10', *args)
            self.assertNotEqual(bad.returncode, 0, args)

    def test_sweep_passes_stress_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '20',
                            '--echo-model', 'residual', '--bot-render', 'recorded', '--rhos', '0.9', '--arm-d',
                            '--energy-k-db', '-12', '--energy-min-speech-ms', '30', *STRESS, '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/5')
            self.assertEqual(res['extra_args'][-2:], list(STRESS))
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D'])


E_ARGS = ('--arm-e', '--erl-estimator', 'vetoed', '--erl-alpha', '0.1', '--erl-margin-db', '6',
          '--erl-min-speech-ms', '30')


class ArmEErlAdaptiveTest(unittest.TestCase):
    """bargein_replay/6: opt-in arm E, energy-ratio gate with an online echo-gain (ERL) estimate."""

    @classmethod
    def setUpClass(cls):
        cls.bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        cls.trials = list(replay.stimuli(40, 39, echo_model='residual', bot_render='recorded',
                                         caller_level_db_range=(-10.0, 0.0)))

    def erl_cfg(self, **overrides):
        return cfg(**({'echo_model': 'residual', 'bot_render': 'recorded', 'energy_k_db': -12.0,
                       'energy_min_speech_ms': 30, 'energy_lag_max_ms': 250, 'erl_estimator': 'quiet',
                       'erl_alpha': 0.1, 'erl_prior_db': -12.0, 'erl_margin_db': 0.0,
                       'erl_min_speech_ms': 30} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_ERL, 'bargein_replay/6')

    def test_gate_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.ErlGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db'}, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith('erl_') or node.attr == 'energy_lag_max_ms', node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, ArmCTest.FORBIDDEN, node.value)
        self.assertEqual(list(inspect.signature(replay.ErlGate.observe).parameters), ['self', 't', 'packet'])
        self.assertEqual(list(inspect.signature(replay.ErlGate.render).parameters), ['self', 't', 'frame'])

    def test_frozen_estimate_matches_d(self):
        # alpha 0 keeps the estimate at the prior; prior -12 margin 0 M30 is exactly arm D k -12 M30
        frozen = self.erl_cfg(erl_alpha=0.0)
        for trial in self.trials:
            d = replay.replay(trial, 'D', frozen, self.bot)
            e = replay.replay(trial, 'E', frozen, self.bot)
            self.assertEqual(d['latency_ms'], e['latency_ms'])
            self.assertEqual(d['false_barge_in'], e['false_barge_in'])
            self.assertEqual(d['trace'].get('adapter_flush_ms'), e['trace'].get('adapter_flush_ms'))
            self.assertEqual(d['trace']['energy_vetoes'], e['trace']['erl_vetoes'])

    def test_estimate_adapts_from_bot_only_windows(self):
        gate = replay.ErlGate(self.erl_cfg(erl_alpha=0.5, erl_min_speech_ms=20), 800)
        loud = array('h', [8000, -8000] * (replay.FRAME_MS * replay.SAMPLES_MS // 2))
        for t in range(100, 400, 20):
            gate.render(t, loud)
        faint = array('h', [400, -400] * (replay.CAPTURE_MS * replay.SAMPLES_MS // 2))  # -26 dB, below VAD
        for t in range(150, 250, 10):
            self.assertFalse(gate.observe(t, faint))
        self.assertLess(gate.est_db, -20)
        self.assertEqual(gate.vetoes, 0)
        # a -12 dB hot packet is vetoed by D's fixed k -12 but is unexplained against the learned -26 dB
        caller = array('h', [2000, -2000] * (replay.CAPTURE_MS * replay.SAMPLES_MS // 2))
        self.assertFalse(gate.observe(250, caller))
        self.assertTrue(gate.observe(260, caller))
        self.assertEqual(gate.vetoes, 0)
        # without adaptation the same packets are vetoed
        fixed = replay.ErlGate(self.erl_cfg(erl_alpha=0.0, erl_min_speech_ms=20), 800)
        for t in range(100, 400, 20):
            fixed.render(t, loud)
        self.assertFalse(fixed.observe(250, caller))
        self.assertFalse(fixed.observe(260, caller))
        self.assertEqual(fixed.vetoes, 2)

    def test_vetoed_estimator_learns_from_vetoed_packets(self):
        loud = array('h', [8000, -8000] * (replay.FRAME_MS * replay.SAMPLES_MS // 2))
        echo = array('h', [2000, -2000] * (replay.CAPTURE_MS * replay.SAMPLES_MS // 2))  # -12 dB, hot
        ests = {}
        for name in ('quiet', 'vetoed'):
            gate = replay.ErlGate(self.erl_cfg(erl_estimator=name, erl_prior_db=-6.0, erl_alpha=0.5), 800)
            for t in range(100, 400, 20):
                gate.render(t, loud)
            gate.observe(200, echo)
            self.assertEqual(gate.vetoes, 1)
            ests[name] = gate.est_db
        self.assertEqual(ests['quiet'], -6.0)
        self.assertLess(ests['vetoed'], -6.0)

    def test_cli_arm_e_marks_v6_and_leaves_abcd_unchanged(self):
        proc, without = run_cli(*ARM_D_F_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, with_e = run_cli(*ARM_D_F_ARGS, *E_ARGS, '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plain, out = json.loads(without), json.loads(with_e)
        self.assertEqual(sha256(without).hexdigest(), ARM_D_F_SHA256)
        self.assertEqual(out['harness'], 'bargein_replay/6')
        self.assertEqual(sorted(out['arms']), ['A', 'B', 'C', 'D', 'E'])
        for arm in ('A', 'B', 'C', 'D'):
            self.assertEqual(canonical(out['arms'][arm]), canonical(plain['arms'][arm]), arm)
        self.assertEqual(out['parameters']['erl_margin_db'], 6.0)
        self.assertEqual(out['parameters']['erl_estimator'], 'vetoed')
        self.assertNotIn('arm_e', out['parameters'])
        self.assertFalse(any(k.startswith('erl_') for k in plain['parameters']))
        summary = json.loads(proc.stdout)
        self.assertIs(summary['aa_identical'], True)
        e = summary['arms']['E']
        self.assertIn('valid_trials_with_veto', e)
        self.assertEqual(sorted(e['final_est_db']), ['n', 'p10', 'p50', 'p90'])
        _, again = run_cli(*ARM_D_F_ARGS, *E_ARGS)
        self.assertEqual(sha256(with_e).hexdigest(), sha256(again).hexdigest())
        bad, _ = run_cli('--trials', '10', '--arm-e', '--erl-min-speech-ms', '25')
        self.assertNotEqual(bad.returncode, 0)

    def test_erl_flags_without_arm_e_change_nothing(self):
        proc, data = run_cli(*ARM_D_F_ARGS, *E_ARGS[1:])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), ARM_D_F_SHA256)

    def test_sweep_passes_arm_e(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '20',
                            '--echo-model', 'residual', '--bot-render', 'recorded', '--rhos', '0.9', '--arm-d',
                            '--energy-k-db', '-12', '--energy-min-speech-ms', '30', *E_ARGS, STRESS[0],
                            '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/6')
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D', 'E'])
            self.assertIn('valid_trials_with_veto', res['arms']['E'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


class ContinuousEchoTest(unittest.TestCase):
    """bargein_replay/7: opt-in --echo-continuous, residual echo in every render-active capture."""

    @classmethod
    def setUpClass(cls):
        cls.trials = list(replay.stimuli(30, 39, echo_model='residual', bot_render='recorded'))
        cls.speaker = array('h', [9000, -9000, 4000, -4000] * (replay.BOT_MS * replay.SAMPLES_MS // 4))

    def res_cfg(self, **overrides):
        return cfg(**({'echo_model': 'residual', 'bot_render': 'recorded'} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_CONTINUOUS, 'bargein_replay/7')

    def test_echo_present_outside_event_window(self):
        for trial in self.trials:
            r = trial['residual']
            start = 400 * replay.SAMPLES_MS  # before every onset (>= 550 ms): no event window here
            plain = replay.capture(trial, self.speaker, start, 160, self.res_cfg())
            cont = replay.capture(trial, self.speaker, start, 160, self.res_cfg(echo_continuous=True))
            self.assertEqual(list(plain), [max(-32768, min(32767, n)) for n in r['noise'][start:start + 160]])
            self.assertNotEqual(list(plain), list(cont), trial['kind'])
            self.assertGreater(sum(v * v for v in cont), 4 * sum(v * v for v in plain), trial['kind'])

    def test_same_path_inside_event_window(self):
        for trial in self.trials:
            r = trial['residual']
            window = trial['echo_ms'] if trial['kind'] == 'echo' else r['double_talk_ms']
            if not window:
                continue
            start = trial['onset'] * replay.SAMPLES_MS
            size = min(window, 60) * replay.SAMPLES_MS
            plain = replay.capture(trial, self.speaker, start, size, self.res_cfg())
            cont = replay.capture(trial, self.speaker, start, size, self.res_cfg(echo_continuous=True))
            self.assertEqual(list(plain), list(cont))

    def test_silent_speaker_adds_no_echo(self):
        silent = array('h', [0]) * len(self.speaker)
        for trial in self.trials[:10]:
            start = 300 * replay.SAMPLES_MS
            self.assertEqual(list(replay.capture(trial, silent, start, 160, self.res_cfg())),
                             list(replay.capture(trial, silent, start, 160, self.res_cfg(echo_continuous=True))))

    def test_labels_unchanged(self):
        bot = replay.voice(replay.BOT_MS, 142, 11000, 115)
        for trial in self.trials[:10]:
            a = replay.replay(trial, 'A', self.res_cfg(), bot)
            b = replay.replay(trial, 'A', self.res_cfg(echo_continuous=True), bot)
            self.assertEqual((a['kind'], a['echo_path'], a['trial']), (b['kind'], b['echo_path'], b['trial']))

    def test_cli_marks_v7_default_bytes_unchanged(self):
        proc, without = run_cli(*ARM_D_F_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(without).hexdigest(), ARM_D_F_SHA256)
        self.assertNotIn('echo_continuous', json.loads(without)['parameters'])
        proc, data = run_cli(*ARM_D_F_ARGS, *E_ARGS, '--echo-continuous', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(data)
        self.assertEqual(out['harness'], 'bargein_replay/7')
        self.assertIs(out['parameters']['echo_continuous'], True)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        _, again = run_cli(*ARM_D_F_ARGS, *E_ARGS, '--echo-continuous')
        self.assertEqual(sha256(data).hexdigest(), sha256(again).hexdigest())
        _, plain_e = run_cli(*ARM_D_F_ARGS, *E_ARGS)
        self.assertEqual(json.loads(plain_e)['harness'], 'bargein_replay/6')
        self.assertNotEqual(sha256(plain_e).hexdigest(), sha256(data).hexdigest())

    def test_cli_rejects_continuous_without_residual(self):
        bad, _ = run_cli('--trials', '10', '--echo-continuous')
        self.assertNotEqual(bad.returncode, 0)

    def test_sweep_passes_echo_continuous(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '20',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos', '0.9',
                            '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30', *E_ARGS, STRESS[0],
                            '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/7')
            self.assertIn('--echo-continuous', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D', 'E'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


# A-bargein-J continuous-echo bytes (seed 39, 20 trials, arms A-E, --echo-continuous), pinned before K's change
CONT_J_ARGS = ARM_D_F_ARGS + E_ARGS + ('--echo-continuous',)
CONT_J_SHA256 = '6668b9a866d5999f42598f51f3ceeb3a83cbad59eb1ec3624f8ba4130e9a6e79'
E2_ARGS = ('--arm-e2', '--erl2-margin-db=6', '--erl2-min-speech-ms', '30')


class AecResidualTest(unittest.TestCase):
    """bargein_replay/8: opt-in --echo-erle-db X (continuous echo after a linear AEC with X dB ERLE) and arm E2
    (unbiased online echo-gain estimate: least-squares fit over all render-active packets at the matched lag)."""

    @classmethod
    def setUpClass(cls):
        cls.trials = list(replay.stimuli(30, 39, echo_model='residual', bot_render='recorded'))
        cls.speaker = array('h', [9000, -9000, 4000, -4000] * (replay.BOT_MS * replay.SAMPLES_MS // 4))

    def res_cfg(self, **overrides):
        return cfg(**({'echo_model': 'residual', 'bot_render': 'recorded', 'echo_continuous': True} | overrides))

    def e2_cfg(self, **overrides):
        return cfg(**({'energy_lag_max_ms': 250, 'erl2_margin_db': 6.0, 'erl2_min_speech_ms': 20,
                       'erl2_prior_db': 0.0, 'erl2_min_updates': 10} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_AEC, 'bargein_replay/8')

    def test_erle_attenuates_echo_only(self):
        start = 400 * replay.SAMPLES_MS
        for trial in self.trials:
            noise = [max(-32768, min(32767, n)) for n in trial['residual']['noise'][start:start + 160]]
            full = replay.capture(trial, self.speaker, start, 160, self.res_cfg())
            zero = replay.capture(trial, self.speaker, start, 160, self.res_cfg(echo_erle_db=0.0))
            att = replay.capture(trial, self.speaker, start, 160, self.res_cfg(echo_erle_db=20.0))
            self.assertEqual(list(full), list(zero))
            e_full = sum((a - b) ** 2 for a, b in zip(full, noise))
            e_att = sum((a - b) ** 2 for a, b in zip(att, noise))
            self.assertGreater(e_full, 0)
            self.assertAlmostEqual(10 * replay.log10(e_full / e_att), 20.0, delta=0.5)

    def test_j_continuous_bytes_unchanged(self):
        proc, data = run_cli(*CONT_J_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)
        self.assertNotIn('echo_erle_db', json.loads(data)['parameters'])

    def test_cli_marks_v8_and_logs_erle(self):
        proc, data = run_cli(*CONT_J_ARGS, '--echo-erle-db', '20', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(data)
        self.assertEqual(out['harness'], 'bargein_replay/8')
        self.assertEqual(out['parameters']['echo_erle_db'], 20.0)
        for row in out['arms']['A']['trials']:
            self.assertEqual(row['echo_path']['erle_db'], 20.0)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        self.assertNotEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_cli_rejects_bad_erle(self):
        bad, _ = run_cli('--trials', '10', '--echo-model', 'residual', '--echo-erle-db', '20')  # no --echo-continuous
        self.assertNotEqual(bad.returncode, 0)
        bad, _ = run_cli('--trials', '10', '--echo-model', 'residual', '--echo-continuous', '--echo-erle-db=-1')
        self.assertNotEqual(bad.returncode, 0)

    def test_e2_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.ErlFitGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db', 'erle_db'},
                                 node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith('erl2_') or node.attr == 'energy_lag_max_ms', node.attr)
        self.assertEqual(list(inspect.signature(replay.ErlFitGate.observe).parameters), ['self', 't', 'packet'])
        self.assertEqual(list(inspect.signature(replay.ErlFitGate.render).parameters), ['self', 't', 'frame'])

    def synthetic(self, gate, gain_db, lag_ms, hot_caller_from=None):
        """Render with a varying envelope; near = render delayed lag_ms scaled by gain_db, + caller after onset."""
        rng = replay.Random(7)
        n = replay.CAPTURE_MS * replay.SAMPLES_MS
        sent = array('h', [0]) * (900 * replay.SAMPLES_MS)
        for t in range(0, 800, 20):
            amp = rng.choice((0, 1500, 4000, 9000, 12000))
            frame = array('h', [amp, -amp] * (replay.FRAME_MS * replay.SAMPLES_MS // 2))
            gate.render(t, frame)
            sent[t * replay.SAMPLES_MS:(t + 20) * replay.SAMPLES_MS] = frame
        g = 10 ** (gain_db / 20)
        fired = None
        for end in range(300, 800, 10):
            src = (end - lag_ms) * replay.SAMPLES_MS
            packet = array('h', (round(g * v) for v in sent[src - n:src]))
            if hot_caller_from is not None and end > hot_caller_from:
                packet = array('h', (v + (15000 if k % 4 < 2 else -15000) for k, v in enumerate(packet)))
            if gate.observe(end, packet) and fired is None:
                fired = end
        return fired

    def test_e2_estimate_unbiased_on_hot_echo(self):
        # echo at -6 dB re render is hot most of the time; the quiet-only E estimator never sees it, E2 must
        for gain_db, lag in ((-6.0, 90), (-20.0, 150), (-30.0, 40)):
            gate = replay.ErlFitGate(self.e2_cfg(), 900)
            self.assertIsNone(self.synthetic(gate, gain_db, lag))
            self.assertAlmostEqual(gate.est_db, gain_db, delta=1.0)
            self.assertEqual(gate.lag_ms, lag)
            self.assertGreater(gate.updates, 40)

    def test_e2_fires_on_caller_over_echo(self):
        gate = replay.ErlFitGate(self.e2_cfg(), 900)
        fired = self.synthetic(gate, -6.0, 90, hot_caller_from=600)
        self.assertIsNotNone(fired)
        self.assertGreater(fired, 600)
        self.assertLessEqual(fired, 640)

    def test_cli_arm_e2_leaves_other_arms_unchanged(self):
        base = CONT_J_ARGS + ('--echo-erle-db', '20')
        proc, plain = run_cli(*base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, with_e2 = run_cli(*base, *E2_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, o = json.loads(plain), json.loads(with_e2)
        self.assertEqual(sorted(o['arms']), ['A', 'B', 'C', 'D', 'E', 'E2'])
        for arm in ('A', 'B', 'C', 'D', 'E'):
            self.assertEqual(canonical(o['arms'][arm]), canonical(p['arms'][arm]), arm)
        self.assertEqual(o['parameters']['erl2_margin_db'], 6.0)
        self.assertFalse(any(k.startswith('erl2_') for k in p['parameters']))
        e2 = json.loads(proc.stdout)['arms']['E2']
        self.assertEqual(sorted(e2['final_est_db']), ['n', 'p10', 'p50', 'p90'])
        self.assertIn('valid_trials_with_veto', e2)
        row = o['arms']['E2']['trials'][0]['trace']
        self.assertIn('erl2_final_est_db', row)
        self.assertIn('erl2_lag_ms', row)
        # E2 flags without --arm-e2 change nothing
        proc, data = run_cli(*CONT_J_ARGS, *E2_ARGS[1:])
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_erle_and_e2(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '20',
                            '--echo-model', 'residual', '--echo-continuous', '--echo-erle-db', '20', '--bot-render',
                            'recorded', '--rhos', '0.9', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms',
                            '30', *E_ARGS, *E2_ARGS, '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/8')
            self.assertIn('--echo-erle-db', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D', 'E', 'E2'])
            self.assertIn('valid_trials_with_veto', res['arms']['E2'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


F_ARGS = ('--arm-f', '--arm-f2', '--energy-k-db', '-12', '--energy-min-speech-ms', '30')


class NlmsGateTest(unittest.TestCase):
    """bargein_replay/9: opt-in arms F / F2, an NLMS linear echo canceller (render reference, uplink input,
    Geigel double-talk freeze) in front of arm D's energy-ratio gate; F vetoes against the render (D's rule on
    the residual), F2 against the canceller's own echo estimate."""

    @classmethod
    def setUpClass(cls):
        cls.render = replay.clip(sorted(replay.ASSETS.glob('*.wav'))[0])

    def f_cfg(self, **overrides):
        return cfg(**({'energy_lag_max_ms': 250, 'energy_k_db': -12.0, 'energy_min_speech_ms': 30,
                       'nlms_mu': 0.5, 'nlms_delta': 1000.0, 'nlms_pre_ms': 15, 'nlms_post_ms': 65,
                       'nlms_geigel': 0.5, 'nlms_hang_ms': 30, 'nlms_min_updates': 10,
                       'nlms_margin_db': 6.0, 'nlms_converged_db': 6.0} | overrides))

    def synthetic(self, gate, lag_ms=130, gain_db=-10.0, caller_from=None, caller_amp=3000, ms=2000):
        """Render = the fixture clip tiled; near = sparse 13-tap echo of the render (lag_ms bulk), + noise,
        + an optional square-ish caller from caller_from ms. Returns the first fire time or None."""
        n = replay.SAMPLES_MS * replay.CAPTURE_MS
        total = ms * replay.SAMPLES_MS
        sent = array('h', (self.render[i % len(self.render)] for i in range(total)))
        for t in range(0, ms, 20):
            gate.render(t, sent[t * replay.SAMPLES_MS:(t + 20) * replay.SAMPLES_MS])
        rng = replay.Random(5)
        taps = [(0, 1.0)] + [(rng.randint(16, 640), rng.choice((-1, 1)) * rng.uniform(.1, .4)) for _ in range(12)]
        g = 10 ** (gain_db / 20)
        bulk = lag_ms * replay.SAMPLES_MS
        fired = None
        for end in range(10, ms + 1, 10):
            packet = array('h', [0]) * n
            for j in range(n):
                i = end * replay.SAMPLES_MS - n + j
                echo = sum(h * sent[i - bulk - d] for d, h in taps if i - bulk - d >= 0)
                v = g * echo + rng.gauss(0, 50)
                if caller_from is not None and end - 10 >= caller_from:
                    v += caller_amp * (1 if (i // 20) % 2 else -1)
                packet[j] = max(-32768, min(32767, round(v)))
            if gate.observe(end, packet) and fired is None:
                fired = end
        return fired

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_NLMS, 'bargein_replay/9')

    def test_f_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.NlmsGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db', 'erle_db',
                                                                  'taps', 'delay_ms_true'}, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith(('nlms_', 'energy_')), node.attr)
        self.assertEqual(list(inspect.signature(replay.NlmsGate.observe).parameters), ['self', 't', 'packet'])
        self.assertEqual(list(inspect.signature(replay.NlmsGate.render).parameters), ['self', 't', 'frame'])

    def test_lowpass_is_unity_dc_and_platform_rounded(self):
        for h in (replay.LOWPASS_16K, replay.LOWPASS_4K):
            self.assertAlmostEqual(sum(h), 1.0, places=6)
            self.assertEqual(list(h), list(reversed(h)))  # linear phase
            self.assertTrue(all(round(c, 9) == c for c in h))

    def test_cancels_sparse_echo_and_finds_delay(self):
        for ref in ('render', 'echo'):
            gate = replay.NlmsGate(self.f_cfg(), 2100, ref)
            self.assertIsNone(self.synthetic(gate), ref)
            self.assertLessEqual(abs(gate.delay_ms - 130), 3, ref)
            late = [10 * replay.log10(c / r) for t, c, r in gate.log if t > 1500 and r > 0]
            self.assertGreater(len(late), 20)
            self.assertGreaterEqual(replay.median(late), 15.0, ref)
            self.assertGreater(gate.updates, 1000)

    def test_geigel_freezes_adaptation_in_double_talk(self):
        gate = replay.NlmsGate(self.f_cfg(), 2100, 'render')
        fired = self.synthetic(gate, caller_from=1200, caller_amp=15000, ms=1400)
        self.assertGreater(gate.dt_packets, 0)
        self.assertIsNotNone(fired)
        self.assertGreater(fired, 1200)
        self.assertLessEqual(fired, 1250)

    def test_f2_fires_on_quiet_caller_over_echo(self):
        # caller ~ -14 dB under the render peak level: arm F (render reference, k -12) vetoes it, F2 does not
        f = replay.NlmsGate(self.f_cfg(), 2100, 'render')
        f2 = replay.NlmsGate(self.f_cfg(), 2100, 'echo')
        fired_f2 = self.synthetic(f2, caller_from=1500, caller_amp=1200, ms=1700)
        self.assertIsNotNone(fired_f2)
        self.assertLessEqual(fired_f2, 1560)
        fired_f = self.synthetic(f, caller_from=1500, caller_amp=1200, ms=1700)
        self.assertTrue(fired_f is None or fired_f > fired_f2)

    def test_cli_arm_f_leaves_other_arms_unchanged(self):
        proc, plain = run_cli(*CONT_J_ARGS)
        self.assertEqual(sha256(plain).hexdigest(), CONT_J_SHA256)
        proc, data = run_cli(*CONT_J_ARGS, *F_ARGS[:2], '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, o = json.loads(plain), json.loads(data)
        self.assertEqual(o['harness'], 'bargein_replay/9')
        self.assertEqual(sorted(o['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2'])
        for arm in ('A', 'B', 'C', 'D', 'E'):
            self.assertEqual(canonical(o['arms'][arm]), canonical(p['arms'][arm]), arm)
        self.assertEqual(o['parameters']['nlms_mu'], 0.5)
        self.assertFalse(any(k.startswith('nlms_') for k in p['parameters']))
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        for arm in ('F', 'F2'):
            s = json.loads(proc.stdout)['arms'][arm]
            self.assertEqual(sorted(s['erle_db']), ['n', 'p10', 'p50', 'p90'])
            self.assertIn('valid_trials_with_veto', s)
            trace = o['arms'][arm]['trials'][0]['trace']
            for key in ('nlms_delay_ms', 'nlms_erle_db', 'nlms_dt_packets', 'nlms_vetoes'):
                self.assertIn(key, trace)
        # F flags without --arm-f / --arm-f2 change nothing
        proc, data = run_cli(*CONT_J_ARGS, '--nlms-mu', '0.5')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_f(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            '0.9', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30', '--arm-f',
                            '--arm-f2', '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/9')
            self.assertIn('--arm-f', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'C0.9', 'D', 'F', 'F2'])
            self.assertIn('valid_trials_with_veto', res['arms']['F'])
            self.assertIn('erle_db', res['arms']['F2'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


# bargein_replay/9 bytes of CONT_J_ARGS + --arm-f --arm-f2 (pinned at 38749e1, before arm G existed)
F9_SHA256 = '032e7b64f6f737f23230c17bc6322ae7050abdafa55175d7e516642f51feed91'
G_ARGS = ('--arm-g', '--arm-g2')


class ResidualGateTest(unittest.TestCase):
    """bargein_replay/10: opt-in arms G / G2, the same NLMS canceller as arm F followed by a residual-only gate
    (local energy onset on the canceller residual, minimum speech energy_min_speech_ms, NO render-referenced
    veto). G2 adds a residual echo suppressor (NLP): the residual energy minus the canceller's own estimate of
    the residual echo (echo estimate energy x 10^(-monitored ERLE/10), over-subtracted by resid_nlp_db)."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def g_cfg(self, **overrides):
        # 1360 = the dev-tuned residual threshold; at 480 the synthetic's not-yet-converged echo fires G at 220 ms
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0}
                                  | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_RESID, 'bargein_replay/10')
        self.assertEqual(replay.HARNESS_NLMS, 'bargein_replay/9')

    def test_g_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.ResidualGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db', 'erle_db',
                                                                  'taps', 'delay_ms_true', 'sent', 'r4', 'r1',
                                                                  'ratio2'}, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith(('nlms_', 'energy_', 'resid_')), node.attr)
        self.assertTrue(issubclass(replay.ResidualGate, replay.NlmsGate))

    def test_g_ignores_echo_only(self):
        for ref in ('residual', 'nlp'):
            gate = replay.ResidualGate(self.g_cfg(), 2100, ref)
            self.assertIsNone(self.nlms.synthetic(gate), ref)
            self.assertLessEqual(abs(gate.delay_ms - 130), 3, ref)
            self.assertEqual(gate.vetoes, 0, ref)  # no veto exists in G / G2

    def test_g_fires_on_quiet_caller_before_f(self):
        # caller ~ -8 dB under the render peak: G (residual only, no veto) fires before arm F (render veto)
        f = replay.NlmsGate(self.nlms.f_cfg(), 2100, 'render')
        fired_f = self.nlms.synthetic(f, caller_from=1500, caller_amp=2400, ms=1800)
        for ref in ('residual', 'nlp'):
            g = replay.ResidualGate(self.g_cfg(), 2100, ref)
            fired_g = self.nlms.synthetic(g, caller_from=1500, caller_amp=2400, ms=1800)
            self.assertIsNotNone(fired_g, ref)
            self.assertTrue(fired_f is None or fired_f > fired_g, (ref, fired_f, fired_g))

    def test_g_threshold_is_the_knob(self):
        low = replay.ResidualGate(self.g_cfg(), 2100, 'residual')
        high = replay.ResidualGate(self.g_cfg(resid_threshold=5000.0), 2100, 'residual')
        self.assertIsNotNone(self.nlms.synthetic(low, caller_from=1500, caller_amp=2400, ms=1800))
        self.assertIsNone(self.nlms.synthetic(high, caller_from=1500, caller_amp=2400, ms=1800))

    def test_g_not_armed_before_canceller_converges(self):
        # a caller from t=0 (before any filter exists) cannot fire G until the canceller is placed and converged
        g = replay.ResidualGate(self.g_cfg(), 2100, 'residual')
        fired = self.nlms.synthetic(g, caller_from=0, caller_amp=1200, ms=300)
        self.assertIsNone(fired)

    def test_cli_arm_g_leaves_other_arms_unchanged(self):
        proc, f9 = run_cli(*CONT_J_ARGS, *F_ARGS[:2])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(f9).hexdigest(), F9_SHA256)  # bargein_replay/9 bytes unchanged
        proc, data = run_cli(*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, o = json.loads(f9), json.loads(data)
        self.assertEqual(o['harness'], 'bargein_replay/10')
        self.assertEqual(sorted(o['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2'):
            self.assertEqual(canonical(o['arms'][arm]), canonical(p['arms'][arm]), arm)
        self.assertEqual(o['parameters']['resid_threshold'], 480.0)
        self.assertFalse(any(k.startswith('resid_') for k in p['parameters']))
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        for arm in ('G', 'G2'):
            s = json.loads(proc.stdout)['arms'][arm]
            self.assertEqual(sorted(s['erle_db']), ['n', 'p10', 'p50', 'p90'])
            self.assertEqual(s['valid_trials_with_veto'], 0)
        # G alone (no F) needs only the canceller flags; resid flags without --arm-g / --arm-g2 change nothing
        proc, data = run_cli(*CONT_J_ARGS, '--arm-g')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(json.loads(data)['arms']), ['A', 'B', 'C', 'D', 'E', 'G'])
        proc, data = run_cli(*CONT_J_ARGS, '--resid-threshold', '300')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_g_without_arm_c(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30', '--arm-f',
                            '--arm-g', '--resid-threshold', '400', '--out', tmp], check=True, capture_output=True,
                           timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/10')
            self.assertIn('--arm-g', res['extra_args'])
            self.assertIn('--resid-threshold', res['extra_args'])
            self.assertNotIn('--arm-c', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'F', 'G'])
            self.assertEqual(list(res['full_output_sha256']), ['no-arm-c'])
            self.assertEqual(res['arms']['G']['valid_trials_with_veto'], 0)
            self.assertTrue(res['ab_identical_across_rho_runs'])


if __name__ == '__main__':
    unittest.main()
