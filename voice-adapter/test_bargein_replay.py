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

    def synthetic(self, gate, lag_ms=130, gain_db=-10.0, caller_from=None, caller_amp=3000, ms=2000, caller_to=None):
        """Render = the fixture clip tiled; near = sparse 13-tap echo of the render (lag_ms bulk), + noise,
        + an optional square-ish caller from caller_from ms (until caller_to ms if given). Returns the first fire
        time or None."""
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
                if caller_from is not None and end - 10 >= caller_from and (caller_to is None or end <= caller_to):
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


# bargein_replay/10 bytes of CONT_J_ARGS + --arm-f --arm-f2 --arm-g --arm-g2 (pinned at 413516b, before arm H existed)
G10_SHA256 = '6f1daa34d45050b40deee7a1e6e0624ac042b82640d8f6d0e58916abc9e707a8'


class TwoPathGateTest(unittest.TestCase):
    """bargein_replay/11: opt-in arm H, arm G's residual-only gate after a double-talk-robust two-path NLMS
    canceller: a background filter adapts on every render-active packet (no Geigel freeze), the foreground filter
    (whose residual the gate reads) never adapts itself and only copies the background when the background's
    residual beat it by twopath_copy_db for twopath_copy_packets consecutive packets."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def h_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'twopath_copy_db': 1.0, 'twopath_copy_packets': 2} | overrides))

    @staticmethod
    def late_erle(gate, t0, t1=10 ** 9):
        late = [10 * replay.log10(c / r) for t, c, r in gate.log if t0 < t <= t1 and r > 0]
        return replay.median(late), len(late)

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_TWOPATH, 'bargein_replay/11')
        self.assertEqual(replay.HARNESS_RESID, 'bargein_replay/10')

    def test_h_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.TwoPathGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db', 'erle_db',
                                                                  'taps', 'delay_ms_true'}, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith(('nlms_', 'energy_', 'resid_', 'twopath_')), node.attr)
        self.assertTrue(issubclass(replay.TwoPathGate, replay.ResidualGate))
        self.assertEqual(list(inspect.signature(replay.TwoPathGate.observe).parameters), ['self', 't', 'packet'])

    def test_h_cancels_echo_and_finds_delay(self):
        gate = replay.TwoPathGate(self.h_cfg(), 2100)
        self.assertIsNone(self.nlms.synthetic(gate))
        self.assertLessEqual(abs(gate.delay_ms - 130), 3)
        erle, n = self.late_erle(gate, 1500)
        self.assertGreater(n, 20)
        self.assertGreaterEqual(erle, 15.0)
        self.assertGreater(gate.copies, 0)
        self.assertEqual(gate.dt_packets, 0)  # no Geigel detector in H

    def test_h_adapts_at_low_erl_where_geigel_freezes(self):
        # ERL 3 dB: Geigel (0.5 x render peak) flags the loud echo itself as double talk, so arm G's canceller
        # barely adapts; H's background keeps adapting and the foreground copies it
        g = replay.ResidualGate(self.h_cfg(resid_threshold=10 ** 6), 2100, 'residual')
        h = replay.TwoPathGate(self.h_cfg(resid_threshold=10 ** 6), 2100)
        self.assertIsNone(self.nlms.synthetic(g, gain_db=-3.0))
        self.assertIsNone(self.nlms.synthetic(h, gain_db=-3.0))
        self.assertGreater(g.dt_packets, 0)
        erle_g, _ = self.late_erle(g, 1500)
        erle_h, _ = self.late_erle(h, 1500)
        self.assertGreaterEqual(erle_h, 15.0)
        self.assertGreater(erle_h, erle_g + 6.0)

    def test_h_foreground_survives_double_talk(self):
        # a loud caller burst after convergence: the background is disturbed, the foreground does not copy it,
        # so cancellation right after the burst is still converged (gate kept from firing by a huge threshold)
        h = replay.TwoPathGate(self.h_cfg(resid_threshold=10 ** 6), 2100)
        self.assertIsNone(self.nlms.synthetic(h, caller_from=1200, caller_to=1400, caller_amp=15000, ms=1700))
        after, n = self.late_erle(h, 1410, 1700)
        self.assertGreater(n, 10)
        self.assertGreaterEqual(after, 12.0)

    def test_h_fires_on_quiet_caller(self):
        h = replay.TwoPathGate(self.h_cfg(), 2100)
        fired = self.nlms.synthetic(h, caller_from=1500, caller_amp=2400, ms=1800)
        self.assertIsNotNone(fired)
        self.assertLessEqual(fired, 1560)

    def test_h_copy_margin_is_the_knob(self):
        # an unreachable copy margin never copies: the foreground stays at zero weights and never arms
        h = replay.TwoPathGate(self.h_cfg(twopath_copy_db=60.0), 2100)
        self.assertIsNone(self.nlms.synthetic(h, caller_from=1500, caller_amp=2400, ms=1800))
        self.assertEqual(h.copies, 0)
        self.assertFalse(h.armed)

    def test_cli_arm_h_leaves_other_arms_unchanged(self):
        proc, g10 = run_cli(*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(g10).hexdigest(), G10_SHA256)  # bargein_replay/10 bytes unchanged
        proc, data = run_cli(*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-h', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, o = json.loads(g10), json.loads(data)
        self.assertEqual(o['harness'], 'bargein_replay/11')
        self.assertEqual(sorted(o['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2'):
            self.assertEqual(canonical(o['arms'][arm]), canonical(p['arms'][arm]), arm)
        self.assertEqual(o['parameters']['twopath_copy_db'], 1.0)
        self.assertFalse(any(k.startswith('twopath_') for k in p['parameters']))
        s = json.loads(proc.stdout)
        self.assertIs(s['aa_identical'], True)
        h = s['arms']['H']
        self.assertEqual(sorted(h['erle_db']), ['n', 'p10', 'p50', 'p90'])
        self.assertEqual(h['valid_trials_with_veto'], 0)
        self.assertIn('copies_per_trial', h)
        self.assertIn('nlms_copies', o['arms']['H']['trials'][0]['trace'])
        # H alone needs only the canceller flags; twopath flags without --arm-h change nothing
        proc, data = run_cli(*CONT_J_ARGS, '--arm-h')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(json.loads(data)['arms']), ['A', 'B', 'C', 'D', 'E', 'H'])
        proc, data = run_cli(*CONT_J_ARGS, '--twopath-copy-db', '3')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_h(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30', '--arm-g',
                            '--arm-h', '--resid-threshold', '1360', '--twopath-copy-db', '2', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/11')
            self.assertIn('--arm-h', res['extra_args'])
            self.assertIn('--twopath-copy-db=2.0', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'G', 'H'])
            self.assertEqual(res['arms']['H']['valid_trials_with_veto'], 0)
            self.assertTrue(res['ab_identical_across_rho_runs'])


# bargein_replay/11 bytes of CONT_J_ARGS + F_ARGS[:2] + G_ARGS + --arm-h (pinned at 8445cc4, before arm O existed)
H11_SHA256 = '28347e01630ecffd673386767c900ea236ba4471ba06727ecbfe4b01ffc96ec3'


class ShadowGuardGateTest(unittest.TestCase):
    """bargein_replay/12: opt-in arm O, arm G unchanged (Geigel-frozen NLMS + residual-only gate) plus a
    Geigel-free shadow NLMS filter used only as a stall/divergence guard: the shadow replaces G's filter only after
    guard_packets consecutive render-active packets with G's monitored ERLE < guard_erle_db and the shadow residual
    >= guard_margin_db lower; until then every decision is arm G's."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def o_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 10} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_SHADOW, 'bargein_replay/12')
        self.assertEqual(replay.HARNESS_TWOPATH, 'bargein_replay/11')

    def test_o_never_reads_labels(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(replay.ShadowGuardGate))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ArmCTest.FORBIDDEN | {'erl_db', 'gain', 'caller_gain_db', 'erle_db',
                                                                  'taps', 'delay_ms_true'}, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == 'cfg':
                    self.assertTrue(node.attr.startswith(('nlms_', 'energy_', 'resid_', 'guard_')), node.attr)
        self.assertTrue(issubclass(replay.ShadowGuardGate, replay.ResidualGate))

    def test_o_equals_g_when_guard_never_fires(self):
        # unreachable guard: every residual, monitor value, decision and fire time is arm G's
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800}):
            g = replay.ResidualGate(self.o_cfg(), 2100, 'residual')
            o = replay.ShadowGuardGate(self.o_cfg(guard_margin_db=60.0), 2100)
            self.assertEqual(self.nlms.synthetic(g, **kw), self.nlms.synthetic(o, **kw), kw)
            self.assertEqual(o.swaps, 0)
            self.assertEqual(o.log, g.log, kw)
            self.assertEqual((o.erle_hat_db, o.dt_packets, o.updates, o.delay_ms),
                             (g.erle_hat_db, g.dt_packets, g.updates, g.delay_ms), kw)

    def test_o_converged_g_is_untouched(self):
        # ERL 10 dB, no double talk: G converges past the 6 dB stall level, the shadow never replaces it
        g = replay.ResidualGate(self.o_cfg(resid_threshold=10 ** 6), 2100, 'residual')
        o = replay.ShadowGuardGate(self.o_cfg(resid_threshold=10 ** 6), 2100)
        self.assertIsNone(self.nlms.synthetic(g))
        self.assertIsNone(self.nlms.synthetic(o))
        self.assertEqual(o.swaps, 0)
        self.assertEqual(o.log, g.log)

    def test_o_replaces_a_stalled_filter_at_low_erl(self):
        # ERL 3 dB: Geigel flags the loud echo itself as double talk, G's filter stalls; O's shadow keeps adapting
        # and the guard swaps it in, so late cancellation is far better than G's
        g = replay.ResidualGate(self.o_cfg(resid_threshold=10 ** 6), 2100, 'residual')
        o = replay.ShadowGuardGate(self.o_cfg(resid_threshold=10 ** 6), 2100)
        self.assertIsNone(self.nlms.synthetic(g, gain_db=-3.0))
        self.assertIsNone(self.nlms.synthetic(o, gain_db=-3.0))
        self.assertGreater(g.dt_packets, 0)
        self.assertGreater(o.swaps, 0)
        erle_g, _ = TwoPathGateTest.late_erle(g, 1500)
        erle_o, n = TwoPathGateTest.late_erle(o, 1500)
        self.assertGreater(n, 20)
        self.assertGreater(erle_o, erle_g + 6.0)

    def test_o_fires_on_caller_like_g(self):
        # with the default (reachable) guard and a converged G, a caller fires O exactly when it fires G
        kw = {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800}
        g = replay.ResidualGate(self.o_cfg(), 2100, 'residual')
        o = replay.ShadowGuardGate(self.o_cfg(guard_packets=5), 2100)
        fired_g = self.nlms.synthetic(g, **kw)
        self.assertIsNotNone(fired_g)
        self.assertEqual(self.nlms.synthetic(o, **kw), fired_g)
        self.assertEqual(o.swaps, 0)

    def test_cli_arm_o_leaves_other_arms_unchanged(self):
        args = (*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-h')
        proc, h11 = run_cli(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(h11).hexdigest(), H11_SHA256)  # bargein_replay/11 bytes unchanged
        proc, data = run_cli(*args, '--arm-o', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, o = json.loads(h11), json.loads(data)
        self.assertEqual(o['harness'], 'bargein_replay/12')
        self.assertEqual(sorted(o['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H'):
            self.assertEqual(canonical(o['arms'][arm]), canonical(p['arms'][arm]), arm)
        self.assertEqual(o['parameters']['guard_packets'], 10)
        self.assertFalse(any(k.startswith('guard_') for k in p['parameters']))
        s = json.loads(proc.stdout)
        self.assertIs(s['aa_identical'], True)
        oa = o['arms']['O']
        self.assertIn('swaps_per_trial', oa)
        self.assertIn('trials_with_swap', oa)
        # per trial: a trial without a swap is byte-identical to arm G's trial (apart from the swap counter)
        same = 0
        for to, tg in zip(oa['trials'], o['arms']['G']['trials']):
            tr = dict(to['trace'])
            if tr.pop('nlms_swaps') == 0:
                self.assertEqual(canonical(dict(to, trace=tr)), canonical(tg), to['trial'])
                same += 1
        self.assertGreater(same, 0)
        # guard flags without --arm-o change nothing
        proc, data = run_cli(*CONT_J_ARGS, '--guard-packets', '3')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_o(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30', '--arm-g',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '7', '--guard-margin-db', '2',
                            '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/12')
            self.assertIn('--arm-o', res['extra_args'])
            self.assertIn('--guard-margin-db=2.0', res['extra_args'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'G', 'O'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


# bargein_replay/12 bytes of CONT_J_ARGS + F_ARGS[:2] + G_ARGS + --arm-h --arm-o (pinned at b7a2ce2, before arm Q)
O12_SHA256 = 'a7ff9cbb879fd4dd0d98bd90423501688f7b9f67b0bd62f520050c51bb4ad563'


class Recorder:
    """Forward render/observe to a duck-confirm gate and record its (t, event) pairs; observe returns False so
    the synthetic drivers run to the end."""

    def __init__(self, gate):
        self.gate = gate
        self.events = []

    def render(self, t, frame):
        self.gate.render(t, frame)

    def observe(self, t, packet):
        event = self.gate.observe(t, packet)
        if event:
            self.events.append((t, event))
        return False


class DuckConfirmGateTest(unittest.TestCase):
    """bargein_replay/14: opt-in arm Q, arm O unchanged (Geigel-frozen NLMS + shadow stall guard + residual-only
    speech decision) except that the 30 ms onset only DUCKS (pauses) playout; the flush is COMMITTED after
    --duck-commit-ms of cumulative residual speech within --duck-window-ms of the duck, else playout resumes
    (un-duck) with no audio lost."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def q_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 200} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_DUCK, 'bargein_replay/14')
        self.assertEqual(replay.HARNESS_SHADOW, 'bargein_replay/12')

    def test_q_is_o_with_duck_commit_knobs_only(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.DuckConfirmGate, replay.ShadowGuardGate))
        tree = ast.parse(inspect.getsource(replay.DuckConfirmGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'duck_commit_ms', 'duck_window_ms'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)

    def test_step_commits_on_cumulative_speech_across_a_gap(self):
        # onset 30 ms ducks; speech 40 ms, a 30 ms gap (a weak syllable), then speech: cumulative 100 ms commits,
        # a consecutive-run counter (arm P) would have reset at the gap
        q = replay.DuckConfirmGate(self.q_cfg(), 2100)
        pattern = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, 1, 1, 1]
        events = [(t, e) for t, s in zip(range(1000, 1200, 10), pattern) if (e := q.step(t, bool(s)))]
        self.assertEqual(events, [(1020, 'duck'), (1120, 'commit')])
        self.assertTrue(q.fired)

    def test_step_unducks_after_the_window_and_can_duck_again(self):
        q = replay.DuckConfirmGate(self.q_cfg(duck_commit_ms=100, duck_window_ms=50), 2100)
        pattern = [1] * 5 + [0] * 5 + [1] * 3
        events = [(t, e) for t, s in zip(range(1000, 1200, 10), pattern) if (e := q.step(t, bool(s)))]
        self.assertEqual(events, [(1020, 'duck'), (1070, 'unduck'), (1120, 'duck')])
        self.assertFalse(q.fired)
        self.assertEqual((q.ducks, q.unducks), (2, 1))

    def test_q_first_duck_is_o_fire(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800},
                   {'caller_from': 1500, 'caller_to': 1560, 'caller_amp': 4000, 'ms': 1800}):
            o = replay.ShadowGuardGate(self.q_cfg(), 2100)
            rec = Recorder(replay.DuckConfirmGate(self.q_cfg(), 2100))
            fired = self.nlms.synthetic(o, **kw)
            self.nlms.synthetic(rec, **kw)
            self.assertEqual(rec.events[0][0] if rec.events else None, fired, kw)

    def test_q_equals_o_when_commit_is_the_onset(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800}):
            o = replay.ShadowGuardGate(self.q_cfg(), 2100)
            rec = Recorder(replay.DuckConfirmGate(self.q_cfg(duck_commit_ms=30), 2100))
            fired = self.nlms.synthetic(o, **kw)
            self.nlms.synthetic(rec, **kw)
            self.assertEqual(rec.events, [] if fired is None else [(fired, 'commit')], kw)
            self.assertEqual((o.log, o.swaps), (rec.gate.log, rec.gate.swaps), kw)

    def test_q_unducks_a_short_burst_that_fires_o(self):
        kw = {'caller_from': 1500, 'caller_to': 1560, 'caller_amp': 4000, 'ms': 1800}
        o = replay.ShadowGuardGate(self.q_cfg(), 2100)
        rec = Recorder(replay.DuckConfirmGate(self.q_cfg(), 2100))
        fired = self.nlms.synthetic(o, **kw)
        self.nlms.synthetic(rec, **kw)
        self.assertIsNotNone(fired)
        self.assertEqual(rec.events[:2], [(fired, 'duck'), (fired + 200, 'unduck')])
        self.assertNotIn('commit', [e for _, e in rec.events])

    def test_q_commits_sustained_speech(self):
        kw = {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}
        o = replay.ShadowGuardGate(self.q_cfg(), 2100)
        rec = Recorder(replay.DuckConfirmGate(self.q_cfg(), 2100))
        fired = self.nlms.synthetic(o, **kw)
        self.nlms.synthetic(rec, **kw)
        self.assertIsNotNone(fired)
        self.assertEqual(rec.events, [(fired, 'duck'), (fired + 70, 'commit')])

    def test_cli_arm_q_leaves_other_arms_unchanged(self):
        args = (*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-h', '--arm-o')
        proc, o12 = run_cli(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(o12).hexdigest(), O12_SHA256)  # bargein_replay/12 bytes unchanged
        proc, data = run_cli(*args, '--arm-q', '--duck-commit-ms', '30', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        b, q = json.loads(o12), json.loads(data)
        self.assertEqual(q['harness'], 'bargein_replay/14')
        self.assertEqual(sorted(q['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O'):
            self.assertEqual(canonical(q['arms'][arm]), canonical(b['arms'][arm]), arm)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        self.assertEqual(q['parameters']['duck_commit_ms'], 30)
        self.assertFalse(any(k.startswith('duck_') for k in b['parameters']))
        # commit at the onset: arm Q is arm O trial by trial (no duck, no un-duck)
        qa, oa = q['arms']['Q'], q['arms']['O']
        for k in ('latency_ms', 'false_barge_ins', 'missed_valid', 'residual_ms', 'negative_by_kind'):
            self.assertEqual(qa[k], oa[k], k)
        self.assertEqual(qa['duck_false_alarms'], 0)
        for tq, to in zip(qa['trials'], oa['trials']):
            self.assertEqual(tq['latency_ms'], to['latency_ms'], tq['trial'])
            self.assertEqual(tq['trace'].get('adapter_flush_ms'), to['trace'].get('adapter_flush_ms'), tq['trial'])
        # a real commit window: ducks first, commits later or resumes; no audio is lost on a resumed burst
        proc, data = run_cli(*args, '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '200')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(data)['arms']
        qa, oa = r['Q'], r['O']
        self.assertLessEqual(qa['false_barge_ins'], oa['false_barge_ins'])
        self.assertIn('duck_false_alarms', qa)
        self.assertIn('commit_latency_ms', qa)
        self.assertIn('swaps_per_trial', qa)
        same = 0
        for tq, to in zip(qa['trials'], oa['trials']):
            ducks = tq['trace'].get('duck_ms', [])
            if 'adapter_flush_ms' in tq['trace']:  # a commit always follows a duck at or after O's fire time
                self.assertIn('adapter_flush_ms', to['trace'], tq['trial'])
                self.assertGreaterEqual(tq['trace']['adapter_flush_ms'], to['trace']['adapter_flush_ms'])
            if ducks:
                self.assertEqual(ducks[0], to['trace']['nlms_fire_ms'], tq['trial'])
            if tq['trace'].get('unduck_ms'):
                self.assertGreater(tq['duck_ms_total'], 0)
            if tq['kind'] == 'valid' and ducks and not tq['trace'].get('unduck_ms') and 'adapter_flush_ms' in \
                    tq['trace']:
                # the first duck commits: the speaker goes silent exactly when O's flush silences it
                self.assertEqual(tq['latency_ms'], to['latency_ms'], tq['trial'])
                same += 1
            if not tq['false_barge_in'] and tq['kind'] != 'valid':
                self.assertEqual(tq['queue_dropped_frames'], 0, tq['trial'])
        self.assertGreater(same, 0)
        # duck flags without --arm-q change nothing; --arm-q needs --arm-o
        proc, data = run_cli(*CONT_J_ARGS, '--duck-commit-ms', '70')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)
        proc, _ = run_cli(*CONT_J_ARGS, '--arm-q')
        self.assertNotEqual(proc.returncode, 0)

    def test_sweep_passes_arm_q(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '90', '--duck-window-ms', '150', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/14')
            self.assertEqual(res['extra_args'][-5:], ['--arm-q', '--duck-commit-ms', '90', '--duck-window-ms', '150'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'O', 'Q'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


# bargein_replay/14 bytes of CONT_J_ARGS + F_ARGS[:2] + G_ARGS + --arm-h --arm-o --arm-q --duck-commit-ms 100
# --duck-window-ms 200 (pinned at 7c7d178, before arm R)
Q14_SHA256 = '33176769bdc722342a7ee4a33ec485b13879346386bcc394fd18b949da0f4558'
Q14_ARGS = (*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-h', '--arm-o', '--arm-q', '--duck-commit-ms', '100',
            '--duck-window-ms', '200')


class SplitDuckGateTest(unittest.TestCase):
    """bargein_replay/15: opt-in arm R, arm Q unchanged (arm O's canceller, onset run, duck/commit/un-duck) except
    that the DUCK (onset) decision uses a lower residual RMS threshold --split-duck-rms; the COMMIT still counts only
    packets with residual RMS >= --resid-threshold (1360)."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def r_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 200, 'split_duck_rms': 900.0}
                                  | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_SPLIT, 'bargein_replay/15')
        self.assertEqual(replay.HARNESS_DUCK, 'bargein_replay/14')

    def test_r_is_q_with_duck_threshold_only(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.SplitDuckGate, replay.DuckConfirmGate))
        tree = ast.parse(inspect.getsource(replay.SplitDuckGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'split_duck_rms'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)

    def test_step_weak_speech_ducks_but_never_commits(self):
        # 30 ms of duck-level (weak) speech ducks; weak speech alone never reaches the commit; the window un-ducks
        r = replay.SplitDuckGate(self.r_cfg(duck_window_ms=100), 2100)
        events = [(t, e) for t in range(1000, 1200, 10) if (e := r.step(t, False, True))]
        self.assertEqual(events[:2], [(1020, 'duck'), (1120, 'unduck')])
        self.assertFalse(r.fired)

    def test_step_commit_counts_only_strong_speech(self):
        # onset: weak, strong, strong (duck at 1020 with 20 ms strong); then strong x 8 -> 100 ms strong at 1100;
        # weak packets after the duck add nothing
        r = replay.SplitDuckGate(self.r_cfg(duck_window_ms=300), 2100)
        pattern = [(0, 1), (1, 1), (1, 1), (0, 1), (0, 1)] + [(1, 1)] * 8
        events = [(t, e) for t, (s, d) in zip(range(1000, 1200, 10), pattern) if (e := r.step(t, bool(s), bool(d)))]
        self.assertEqual(events, [(1020, 'duck'), (1120, 'commit')])
        self.assertTrue(r.fired)

    def test_step_gap_in_duck_speech_resets_the_onset(self):
        r = replay.SplitDuckGate(self.r_cfg(), 2100)
        pattern = [1, 1, 0, 1, 1, 1]
        events = [(t, e) for t, d in zip(range(1000, 1100, 10), pattern) if (e := r.step(t, False, bool(d)))]
        self.assertEqual(events, [(1050, 'duck')])

    def test_r_equals_q_at_the_commit_threshold(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800},
                   {'caller_from': 1500, 'caller_to': 1560, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}):
            q = Recorder(replay.DuckConfirmGate(self.r_cfg(), 2100))
            r = Recorder(replay.SplitDuckGate(self.r_cfg(split_duck_rms=1360.0), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(r, **kw)
            self.assertEqual(r.events, q.events, kw)
            self.assertEqual((q.gate.log, q.gate.swaps), (r.gate.log, r.gate.swaps), kw)

    def test_r_ducks_no_later_than_q(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}):
            q = Recorder(replay.DuckConfirmGate(self.r_cfg(), 2100))
            r = Recorder(replay.SplitDuckGate(self.r_cfg(), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(r, **kw)
            if q.events:
                self.assertTrue(r.events, kw)
                self.assertLessEqual(r.events[0][0], q.events[0][0], kw)
            k = min(len(q.gate.log), len(r.gate.log))  # canceller untouched (each stops at its own commit)
            self.assertGreater(k, 0, kw)
            self.assertEqual(q.gate.log[:k], r.gate.log[:k], kw)

    def test_r_ducks_a_caller_too_weak_for_q(self):
        found = []
        for amp in (500, 700, 900, 1100, 1300, 1600, 2000):
            kw = {'caller_from': 1500, 'caller_amp': amp, 'ms': 1800}
            q = Recorder(replay.DuckConfirmGate(self.r_cfg(), 2100))
            r = Recorder(replay.SplitDuckGate(self.r_cfg(split_duck_rms=700.0), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(r, **kw)
            if not q.events and r.events:
                found.append(amp)
        self.assertTrue(found)

    def test_cli_arm_r_leaves_other_arms_unchanged(self):
        proc, q14 = run_cli(*Q14_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(q14).hexdigest(), Q14_SHA256)  # bargein_replay/14 bytes unchanged
        # Q14_ARGS leave --resid-threshold at its default (480): the duck threshold equal to it is arm Q
        proc, data = run_cli(*Q14_ARGS, '--arm-r', '--split-duck-rms', '480', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        b, r = json.loads(q14), json.loads(data)
        self.assertEqual(r['harness'], 'bargein_replay/15')
        self.assertEqual(sorted(r['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q', 'R'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q'):
            self.assertEqual(canonical(r['arms'][arm]), canonical(b['arms'][arm]), arm)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        self.assertEqual(r['parameters']['split_duck_rms'], 480.0)
        self.assertFalse(any(k.startswith('split_') for k in b['parameters']))
        # duck threshold == commit threshold: arm R is arm Q trial by trial (whole arm summary and every row)
        self.assertEqual(canonical(r['arms']['R']), canonical(r['arms']['Q']))
        # the slot's commit threshold 1360: duck_rms 1360 is arm Q trial by trial
        proc, data = run_cli(*CONT_J_ARGS, '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5',
                             '--arm-q', '--arm-r', '--split-duck-rms', '1360')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r1360 = json.loads(data)['arms']
        self.assertEqual(canonical(r1360['R']), canonical(r1360['Q']))
        # a lower duck threshold: ducks no later than Q; commits only on strong speech
        proc, data = run_cli(*Q14_ARGS, '--arm-r', '--split-duck-rms', '300')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        ra, qa = arms['R'], arms['Q']
        self.assertEqual(ra['latency_definition'], 'time_to_ducked_or_silent')
        self.assertGreaterEqual(ra['ducks'], qa['ducks'])
        for tr, tq in zip(ra['trials'], qa['trials']):
            dr, dq = tr['trace'].get('duck_ms', []), tq['trace'].get('duck_ms', [])
            if dq:
                self.assertTrue(dr, tr['trial'])
                self.assertLessEqual(dr[0], dq[0], tr['trial'])
            if not tr['false_barge_in'] and tr['kind'] != 'valid':
                self.assertEqual(tr['queue_dropped_frames'], 0, tr['trial'])
        # split flags without --arm-r change nothing; --arm-r needs --arm-q; duck threshold <= commit threshold
        proc, data = run_cli(*Q14_ARGS, '--split-duck-rms', '300')
        self.assertEqual(sha256(data).hexdigest(), Q14_SHA256)
        proc, _ = run_cli(*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-o', '--arm-r', '--split-duck-rms', '300')
        self.assertNotEqual(proc.returncode, 0)
        proc, _ = run_cli(*Q14_ARGS, '--arm-r', '--split-duck-rms', '600')  # above the commit threshold
        self.assertNotEqual(proc.returncode, 0)
        proc, _ = run_cli(*Q14_ARGS, '--arm-r')  # the duck threshold has no default
        self.assertNotEqual(proc.returncode, 0)

    def test_default_and_cont_j_bytes_unchanged(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*CONT_J_ARGS, '--split-duck-rms', '900')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_r(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '90', '--duck-window-ms', '150', '--arm-r',
                            '--split-duck-rms', '1000', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/15')
            self.assertEqual(res['extra_args'][-3:], ['--arm-r', '--split-duck-rms', '1000.0'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'O', 'Q', 'R'])
            self.assertTrue(res['ab_identical_across_rho_runs'])


R15_ARGS = (*Q14_ARGS, '--arm-r', '--split-duck-rms', '480')
R15_SHA256 = '52e1e78dfee7e521c921632c4ac95b2ff33d863ea50feca78531ea3a02d51404'  # computed on the unchanged /15 harness


class PreArmGateTest(unittest.TestCase):
    """bargein_replay/16: opt-in arm S, arm Q unchanged except that while the gate is NOT yet armed (filter not placed
    or its ERLE monitor never reached --resid-arm-db), a packet whose residual RMS >= --prearm-rms also counts as
    speech for Q's onset/duck run and commit count. Once armed, arm Q exactly."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def s_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 2000.0}
                                  | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_PREARM, 'bargein_replay/16')
        self.assertEqual(replay.HARNESS_SPLIT, 'bargein_replay/15')

    def test_s_is_q_with_prearm_only(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.PreArmGate, replay.DuckConfirmGate))
        tree = ast.parse(inspect.getsource(replay.PreArmGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'prearm_rms'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)

    def test_decide_unarmed_uses_prearm_threshold_then_q(self):
        g = replay.PreArmGate(self.s_cfg(), 2100)
        n = 40
        self.assertFalse(g.decide(n, n * 1900.0 ** 2, 0.0, 0.0, 0.0))  # unarmed, below U
        self.assertFalse(g.speech)
        self.assertTrue(g.decide(n, n * 2000.0 ** 2, 0.0, 0.0, 0.0))  # unarmed, at U
        self.assertTrue(g.speech)
        self.assertFalse(g.armed)
        self.assertEqual(g.prearm_packets, 1)
        g.delay_ms, g.erle_hat_db = 100, 7.0  # placed and monitor >= 6 dB: arms (latched) on the next decide
        self.assertTrue(g.decide(n, n * 1400.0 ** 2, 0.0, 0.0, 0.0))  # armed: Q's 1360 rule
        self.assertTrue(g.armed)
        self.assertFalse(g.decide(n, n * 1300.0 ** 2, 0.0, 0.0, 0.0))
        g.erle_hat_db = 0.0  # latched: stays armed, the pre-arm rule never comes back
        self.assertFalse(g.decide(n, n * 1300.0 ** 2, 0.0, 0.0, 0.0))
        self.assertEqual(g.prearm_packets, 1)

    def test_s_equals_q_when_prearm_never_reached(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 300, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 2400, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}):
            q = Recorder(replay.DuckConfirmGate(self.s_cfg(), 2100))
            s = Recorder(replay.PreArmGate(self.s_cfg(prearm_rms=1e9), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(s, **kw)
            self.assertEqual(s.events, q.events, kw)
            self.assertEqual((q.gate.log, q.gate.swaps), (s.gate.log, s.gate.swaps), kw)

    def test_s_catches_an_early_caller_q_misses(self):
        found = []
        for start in (0, 100, 200, 300, 400):
            kw = {'caller_from': start, 'caller_amp': 5000, 'ms': 1800}
            q = Recorder(replay.DuckConfirmGate(self.s_cfg(), 2100))
            s = Recorder(replay.PreArmGate(self.s_cfg(prearm_rms=3000.0), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(s, **kw)
            if not any(e == 'commit' for _, e in q.events) and any(e == 'commit' for _, e in s.events):
                found.append(start)
        self.assertTrue(found)

    def test_cli_arm_s_leaves_other_arms_unchanged(self):
        proc, r15 = run_cli(*R15_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(r15).hexdigest(), R15_SHA256)  # bargein_replay/15 bytes unchanged
        proc, data = run_cli(*R15_ARGS, '--arm-s', '--prearm-rms', '1000000', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        b, s = json.loads(r15), json.loads(data)
        self.assertEqual(s['harness'], 'bargein_replay/16')
        self.assertEqual(sorted(s['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q', 'R', 'S'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q', 'R'):
            self.assertEqual(canonical(s['arms'][arm]), canonical(b['arms'][arm]), arm)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        self.assertEqual(s['parameters']['prearm_rms'], 1000000.0)
        self.assertFalse(any(k.startswith('prearm_') for k in b['parameters']))
        # a pre-arm threshold no packet reaches: arm S is arm Q trial by trial
        sa = dict(s['arms']['S'])
        self.assertEqual(sa.pop('prearm_speech_packets'), 0)
        self.assertEqual(canonical(sa), canonical(s['arms']['Q']))
        # a reachable pre-arm threshold: S ducks no later than Q (pre-arm speech only adds speech packets)
        proc, data = run_cli(*R15_ARGS, '--arm-s', '--prearm-rms', '2000')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        sa, qa = arms['S'], arms['Q']
        self.assertEqual(sa['latency_definition'], 'time_to_ducked_or_silent')
        self.assertLessEqual(sa['missed_valid'], qa['missed_valid'])
        # prearm flag without --arm-s changes nothing; --arm-s needs --arm-q and --prearm-rms >= --resid-threshold
        proc, data = run_cli(*R15_ARGS, '--prearm-rms', '2000')
        self.assertEqual(sha256(data).hexdigest(), R15_SHA256)
        proc, _ = run_cli(*CONT_J_ARGS, *F_ARGS[:2], *G_ARGS, '--arm-o', '--arm-s', '--prearm-rms', '2000')
        self.assertNotEqual(proc.returncode, 0)
        proc, _ = run_cli(*Q14_ARGS, '--arm-s', '--prearm-rms', '400')  # below the commit threshold (480)
        self.assertNotEqual(proc.returncode, 0)
        proc, _ = run_cli(*Q14_ARGS, '--arm-s')  # the pre-arm threshold has no default
        self.assertNotEqual(proc.returncode, 0)

    def test_default_and_cont_j_bytes_unchanged(self):
        proc, data = run_cli('--seed', '39', '--prearm-rms', '2000')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*CONT_J_ARGS, '--prearm-rms', '2000')
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)

    def test_sweep_passes_arm_s(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--arm-d', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '300', '--arm-s',
                            '--prearm-rms', '3000', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/16')
            self.assertEqual(res['extra_args'][-3:], ['--arm-s', '--prearm-rms', '3000.0'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'D', 'O', 'Q', 'S'])


S16_ARGS = (*R15_ARGS, '--arm-s', '--prearm-rms', '2000')
S16_SHA256 = '264e88cd9910c30198070f96b3450799fe078593cac2a3c9a4949defcd60ed1d'  # computed on the unchanged /16 harness


class ArmRecorder(Recorder):
    """Recorder that also notes whether the gate was armed when each event fired."""

    def observe(self, t, packet):
        event = self.gate.observe(t, packet)
        if event:
            self.events.append((t, event, self.gate.armed))
        return False


class NoDuckPreArmGateTest(unittest.TestCase):
    """bargein_replay/18: opt-in arm U, arm S unchanged (pre-arm speech rule, canceller, arming, commit count and
    window) except that playout is never ducked (paused) while the gate is not yet armed: an unarmed onset run opens
    a silent window in which speech still counts toward the commit; the first duck can only happen once armed."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def u_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 2000.0}
                                  | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_NODUCK, 'bargein_replay/18')
        self.assertEqual(replay.HARNESS_PREARM, 'bargein_replay/16')

    def test_u_is_s_with_no_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.NoDuckPreArmGate, replay.PreArmGate))
        tree = ast.parse(inspect.getsource(replay.NoDuckPreArmGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, set())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)

    def test_step_unarmed_never_ducks_but_commits_on_evidence(self):
        g = replay.NoDuckPreArmGate(self.u_cfg(), 2100)
        self.assertFalse(g.armed)
        events = [g.step(t, True) for t in range(10, 110, 10)]
        self.assertNotIn('duck', events)
        self.assertEqual(events[-1], 'commit')  # 100 ms of pre-arm speech commits without any duck
        self.assertEqual(events[:-1], [None] * 9)
        self.assertEqual(g.ducks, 0)
        self.assertEqual(g.silent_windows, 1)

    def test_step_unarmed_window_lapses_silently(self):
        g = replay.NoDuckPreArmGate(self.u_cfg(), 2100)
        events = [g.step(t, t <= 40) for t in range(10, 400, 10)]  # 40 ms burst, then nothing
        self.assertEqual(set(events), {None})  # no duck, no unduck, no commit
        self.assertEqual((g.ducks, g.unducks, g.fired), (0, 0, False))
        self.assertFalse(g.ducked)
        self.assertEqual(g.silent_windows, 1)

    def test_step_armed_is_q(self):
        u = replay.NoDuckPreArmGate(self.u_cfg(), 2100)
        q = replay.DuckConfirmGate(self.u_cfg(), 2100)
        u.armed = q.armed = True
        pattern = [1, 1, 1, 0, 0, 1, 0] + [0] * 40 + [1] * 12
        eu = [u.step(10 * (i + 1), bool(v)) for i, v in enumerate(pattern)]
        eq = [q.step(10 * (i + 1), bool(v)) for i, v in enumerate(pattern)]
        self.assertEqual(eu, eq)
        self.assertIn('duck', eu)
        self.assertIn('unduck', eu)

    def test_step_arming_inside_silent_window_ducks_and_keeps_evidence(self):
        g = replay.NoDuckPreArmGate(self.u_cfg(), 2100)
        events = [g.step(t, True) for t in (10, 20, 30, 40)]  # unarmed: 40 ms counted, silent window open
        self.assertEqual(events, [None] * 4)
        g.armed = True
        self.assertEqual(g.step(50, True), 'duck')  # armed speech inside the window: duck now
        self.assertEqual(g.ducks, 1)
        out = [g.step(t, True) for t in (60, 70, 80, 90, 100)]
        self.assertEqual(out[-1], 'commit')  # the pre-arm 40 ms still counted toward the 100 ms
        self.assertEqual(out[:-1], [None] * 4)

    def test_u_never_ducks_before_armed_on_synthetics(self):
        for kw in ({}, {'gain_db': -3.0}, {'gain_db': 0.0}, {'caller_from': 0, 'caller_amp': 5000, 'ms': 1800},
                   {'caller_from': 200, 'caller_amp': 5000, 'ms': 1800},
                   {'caller_from': 300, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}):
            u = ArmRecorder(replay.NoDuckPreArmGate(self.u_cfg(prearm_rms=1500.0), 2100))
            self.nlms.synthetic(u, **kw)
            for t, event, armed in u.events:
                if event in ('duck', 'unduck'):
                    self.assertTrue(armed, (kw, t, event))

    def test_u_equals_q_when_prearm_never_reached(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 300, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 6000, 'ms': 1800}):
            q = Recorder(replay.DuckConfirmGate(self.u_cfg(), 2100))
            u = Recorder(replay.NoDuckPreArmGate(self.u_cfg(prearm_rms=1e9), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(u, **kw)
            self.assertEqual(u.events, q.events, kw)

    def test_u_commits_an_early_caller_q_misses(self):
        found = []
        for start in (0, 100, 200, 300, 400):
            kw = {'caller_from': start, 'caller_amp': 5000, 'ms': 1800}
            q = Recorder(replay.DuckConfirmGate(self.u_cfg(), 2100))
            u = ArmRecorder(replay.NoDuckPreArmGate(self.u_cfg(prearm_rms=3000.0), 2100))
            self.nlms.synthetic(q, **kw)
            self.nlms.synthetic(u, **kw)
            if not any(e == 'commit' for _, e in q.events) and any(e == 'commit' for _, e, _ in u.events):
                found.append(start)
        self.assertTrue(found)

    def test_cli_arm_u_leaves_other_arms_unchanged(self):
        proc, s16 = run_cli(*S16_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(s16).hexdigest(), S16_SHA256)  # bargein_replay/16 bytes unchanged
        proc, data = run_cli(*S16_ARGS, '--arm-u', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        b, u = json.loads(s16), json.loads(data)
        self.assertEqual(u['harness'], 'bargein_replay/18')
        self.assertEqual(sorted(u['arms']), ['A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q', 'R', 'S',
                                             'U'])
        for arm in ('A', 'B', 'C', 'D', 'E', 'F', 'F2', 'G', 'G2', 'H', 'O', 'Q', 'R', 'S'):
            self.assertEqual(canonical(u['arms'][arm]), canonical(b['arms'][arm]), arm)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        self.assertEqual(u['parameters'], b['parameters'])  # no new parameter
        ua = u['arms']['U']
        self.assertEqual(ua['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('prearm_speech_packets', ua)
        self.assertIn('prearm_silent_windows', ua)
        for row in ua['trials']:  # no duck while unarmed: every duck is at or after the arming time
            ducks = row['trace'].get('duck_ms', [])
            if ducks:
                self.assertIsNotNone(row['trace']['nlms_armed_ms'], row['trial'])
                self.assertGreaterEqual(ducks[0], row['trace']['nlms_armed_ms'], row['trial'])
        # a pre-arm threshold no packet reaches: arm U is arm Q trial by trial
        proc, data = run_cli(*R15_ARGS, '--arm-s', '--prearm-rms', '1000000', '--arm-u')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        ua = dict(arms['U'])
        self.assertEqual(ua.pop('prearm_speech_packets'), 0)
        self.assertEqual(ua.pop('prearm_silent_windows'), 0)
        qa = arms['Q']
        for rq, ru in zip(qa['trials'], ua.pop('trials')):
            ru = dict(ru)
            ru['trace'] = {k: v for k, v in ru['trace'].items() if k != 'nlms_armed_ms'}
            self.assertEqual(canonical(ru), canonical(rq), rq['trial'])
        self.assertEqual(canonical(ua), canonical({k: v for k, v in qa.items() if k != 'trials'}))
        # --arm-u without --arm-s is an error
        proc, _ = run_cli(*R15_ARGS, '--arm-u')
        self.assertNotEqual(proc.returncode, 0)

    def test_default_and_cont_j_bytes_unchanged(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*CONT_J_ARGS)
        self.assertEqual(sha256(data).hexdigest(), CONT_J_SHA256)
        proc, data = run_cli(*R15_ARGS)
        self.assertEqual(sha256(data).hexdigest(), R15_SHA256)

    def test_sweep_passes_arm_u(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '300', '--arm-s',
                            '--prearm-rms', '3000', '--arm-u', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/18')
            self.assertEqual(res['extra_args'][-4:], ['--arm-s', '--prearm-rms', '3000.0', '--arm-u'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'O', 'Q', 'S', 'U'])


U18_ARGS = ('--seed', '39', '--trials', '20', '--echo-model', 'residual', '--echo-continuous', '--bot-render',
            'recorded', '--energy-k-db=-12', '--energy-min-speech-ms', '30', '--resid-threshold', '1360', '--arm-o',
            '--guard-packets', '5', '--arm-q', '--duck-window-ms', '300', '--arm-s', '--prearm-rms', '5000', '--arm-u',
            '--caller-level-db-range=-10,0')
U18_SHA256 = 'f272e708ada3a5f2c15be7457a4b3e39127bf03083ff33d93bc2e33b66fdcaff'  # computed on the unchanged /18 harness


class QuietCallerGateTest(unittest.TestCase):
    """bargein_replay/19: opt-in arm W, arm U unchanged except ONE commit-side rule: once armed, a packet whose
    residual RMS is >= quiet_rms (below resid_threshold) and not below the capture adds to the commit count while a
    duck or silent window is open (never starts one). Plus --only-arms, a run filter that skips opt-in arms without
    touching the arms that run."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def w_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'quiet_rms': 600.0} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_QUIET, 'bargein_replay/19')
        self.assertEqual(replay.HARNESS_NODUCK, 'bargein_replay/18')

    def test_w_is_u_subclass_reading_one_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.QuietCallerGate, replay.NoDuckPreArmGate))
        tree = ast.parse(inspect.getsource(replay.QuietCallerGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'quiet_rms'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)

    def test_decide_flags_quiet_packets_without_calling_them_speech(self):
        n = 40
        lvl = lambda rms: n * rms * rms
        g = replay.QuietCallerGate(self.w_cfg(), 2100)
        g.delay_ms, g.erle_hat_db = 30, 10.0  # placed, monitor above resid_arm_db: arms on the first decide
        # quiet residual the canceller did not reduce (residual == capture): flagged quiet, not speech for the onset
        self.assertFalse(g.decide(n, lvl(800), lvl(800), 0.0, 0.0))
        self.assertTrue(g.armed and g.quiet)
        # residual above the capture (canceller added energy): quiet as well
        g.decide(n, lvl(800), lvl(700), 0.0, 0.0)
        self.assertTrue(g.quiet)
        # same residual level but the canceller removed 1 dB / 10 dB: not quiet
        g.decide(n, lvl(800), lvl(800) * 10 ** .1, 0.0, 0.0)
        self.assertFalse(g.quiet)
        g.decide(n, lvl(800), lvl(800) * 10.0, 0.0, 0.0)
        self.assertFalse(g.quiet)
        # unexplained but below the floor
        g.decide(n, lvl(500), lvl(500), 0.0, 0.0)
        self.assertFalse(g.quiet)
        # above resid_threshold: speech by arm Q's rule, not quiet
        self.assertTrue(g.decide(n, lvl(1500), lvl(1500) * 100, 0.0, 0.0))
        self.assertFalse(g.quiet)
        # unarmed (not placed): never quiet; pre-arm rule (5000) not reached either
        h = replay.QuietCallerGate(self.w_cfg(), 2100)
        self.assertFalse(h.decide(n, lvl(800), lvl(800), 0.0, 0.0))
        self.assertFalse(h.armed or h.quiet)

    def test_step_quiet_counts_only_inside_an_open_duck(self):
        u = replay.NoDuckPreArmGate(self.w_cfg(), 2100)
        w = replay.QuietCallerGate(self.w_cfg(), 2100)
        u.armed = w.armed = True
        # quiet packets alone never start an onset: no duck, no commit
        w.quiet = True
        self.assertEqual({w.step(t, False) for t in range(10, 200, 10)}, {None})
        self.assertEqual((w.ducks, w.quiet_packets, w.fired), (0, 0, False))
        # 30 ms of strong speech ducks both; then the caller fades under resid_threshold (quiet, unexplained)
        ev_u = [u.step(t, True) for t in (210, 220, 230)]
        ev_w = [w.step(t, True) for t in (210, 220, 230)]
        self.assertEqual(ev_u[-1], 'duck')
        self.assertEqual(ev_w, ev_u)
        rest_u, rest_w = [], []
        for t in range(240, 560, 10):
            rest_u.append(u.step(t, False))
            w.quiet = True
            rest_w.append(w.step(t, False))
        self.assertIn('unduck', rest_u)  # U: 30 ms of evidence, window lapses
        self.assertNotIn('commit', rest_u)
        self.assertIn('commit', rest_w)  # W: quiet packets fill the commit count to 100 ms
        self.assertEqual(rest_w.index('commit'), 6)  # 30 + 7 x 10 = 100 ms at t = 300
        self.assertEqual(w.quiet_packets, 7)

    def test_w_equals_u_when_floor_is_resid_threshold(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 300, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 1500, 'ms': 1800}):
            u = Recorder(replay.NoDuckPreArmGate(self.w_cfg(), 2100))
            w = Recorder(replay.QuietCallerGate(self.w_cfg(quiet_rms=1360.0), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(w, **kw)
            self.assertEqual(w.events, u.events, kw)

    def test_w_never_ducks_earlier_than_u_on_synthetics(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 1200, 'caller_amp': 1100, 'ms': 2000},
                   {'caller_from': 1200, 'caller_amp': 3000, 'ms': 2000}):
            # no pre-arm level (no silent windows): the first duck comes from arm Q's onset run in both arms
            u = Recorder(replay.NoDuckPreArmGate(self.w_cfg(prearm_rms=1e9), 2100))
            w = Recorder(replay.QuietCallerGate(self.w_cfg(prearm_rms=1e9), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(w, **kw)
            first = lambda r: next((t for t, e in r.events if e in ('duck', 'commit')), None)
            self.assertEqual(first(w), first(u), kw)  # the onset is U's; only the commit side can differ

    def test_w_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)  # bargein_replay/18 bytes unchanged
        proc, data = run_cli(*R15_ARGS)
        self.assertEqual(sha256(data).hexdigest(), R15_SHA256)

    def test_cli_arm_w_leaves_other_arms_unchanged(self):
        proc, base = run_cli(*U18_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli(*U18_ARGS, '--arm-w', '--quiet-rms', '700', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, w = json.loads(base), json.loads(data)
        self.assertEqual(w['harness'], 'bargein_replay/19')
        self.assertEqual(sorted(w['arms']), ['A', 'B', 'O', 'Q', 'S', 'U', 'W'])
        for arm in ('A', 'B', 'O', 'Q', 'S', 'U'):
            self.assertEqual(canonical(w['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(w['parameters'])
        self.assertEqual(params.pop('quiet_rms'), 700.0)
        self.assertEqual(params, b['parameters'])
        wa = w['arms']['W']
        self.assertEqual(wa['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('quiet_speech_packets', wa)
        self.assertIn('prearm_silent_windows', wa)
        # floor == resid_threshold: arm W is arm U trial by trial
        proc, data = run_cli(*U18_ARGS, '--arm-w', '--quiet-rms', '1360')
        arms = json.loads(data)['arms']
        wa, ua = dict(arms['W']), dict(arms['U'])
        self.assertEqual(wa.pop('quiet_speech_packets'), 0)
        self.assertEqual(canonical(wa), canonical(ua))
        for bad in ((*U18_ARGS, '--arm-w'), (*U18_ARGS, '--arm-w', '--quiet-rms', '2000'),
                    (*U18_ARGS[:-2], '--caller-level-db-range=-10,0', '--arm-w', '--quiet-rms', '-1')):
            proc, _ = run_cli(*bad)
            self.assertNotEqual(proc.returncode, 0, bad)
        args = [a for a in U18_ARGS if a != '--arm-u']  # --arm-w needs --arm-u
        proc, _ = run_cli(*args, '--arm-w', '--quiet-rms', '700')
        self.assertNotEqual(proc.returncode, 0)

    def test_only_arms_skips_without_changing_the_rest(self):
        proc, full = run_cli(*U18_ARGS, '--arm-w', '--quiet-rms', '700')
        proc, part = run_cli(*U18_ARGS, '--arm-w', '--quiet-rms', '700', '--only-arms', 'U,W')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        f, p = json.loads(full), json.loads(part)
        self.assertEqual(sorted(p['arms']), ['A', 'B', 'U', 'W'])
        for arm in ('A', 'B', 'U', 'W'):
            self.assertEqual(canonical(p['arms'][arm]), canonical(f['arms'][arm]), arm)
        self.assertEqual(p['parameters'], f['parameters'])  # a run filter, not a parameter
        self.assertEqual(p['harness'], f['harness'])
        proc, _ = run_cli(*U18_ARGS, '--only-arms', 'U,V')  # V is no arm (Z is arm Z since bargein_replay/23)
        self.assertNotEqual(proc.returncode, 0)

    def test_sweep_passes_arm_w_and_only_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '300', '--arm-s',
                            '--prearm-rms', '5000', '--arm-u', '--arm-w', '--quiet-rms', '700', '--only-arms', 'U,W',
                            '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/19')
            self.assertEqual(res['extra_args'][-6:], ['--arm-u', '--arm-w', '--quiet-rms', '700.0', '--only-arms', 'U,W'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'W'])


W19_ARGS = (*U18_ARGS, '--arm-w', '--quiet-rms', '700', '--only-arms', 'U,W')
W19_SHA256 = 'd0ccb2c2e354e856741b53987905a690681d48b43aba140b28ff0303b7e159b2'  # computed on the unchanged /19 harness


class ErleFreezeGateTest(unittest.TestCase):
    """bargein_replay/20: opt-in arm X, arm U unchanged except ONE adaptation-control rule: once armed (filter
    placed), a render-active packet whose capture is above the VAD level and whose a-priori residual (the current
    filter, before this packet adapts it) leaves an ERLE more than dtfreeze_db below the canceller's own long-term
    ERLE monitor freezes adaptation (Geigel's freeze and hangover) for that packet. No speech, onset or commit rule
    changes; only the residual the unchanged arm-U decision reads."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def x_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'dtfreeze_db': 6.0} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_DTFREEZE, 'bargein_replay/20')
        self.assertEqual(replay.HARNESS_QUIET, 'bargein_replay/19')

    def test_x_is_u_subclass_reading_one_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.ErleFreezeGate, replay.NoDuckPreArmGate))
        self.assertFalse(issubclass(replay.ErleFreezeGate, replay.QuietCallerGate))
        tree = ast.parse(inspect.getsource(replay.ErleFreezeGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'dtfreeze_db'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
        # no decision code: X never overrides decide() or step(), so speech/onset/commit rules are U's
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn('decide', names)
        self.assertNotIn('step', names)

    def test_freeze_check_rule(self):
        g = replay.ErleFreezeGate(self.x_cfg(), 2100)
        n = 40
        hot = n * replay.THRESHOLD ** 2
        g.erle_hat_db = 14.0
        # a-priori ERLE 4 dB < 14 - 6: freeze; 10 dB: no; capture under the VAD level: no; render idle: no
        self.assertTrue(g.freeze_check(n, ed=hot * 10, ee=hot * 10 / 10 ** .4, top=hot * 4))
        self.assertFalse(g.freeze_check(n, ed=hot * 10, ee=hot * 10 / 10 ** 1.0, top=hot * 4))
        self.assertFalse(g.freeze_check(n, ed=hot * .5, ee=hot * .5, top=hot * 4))
        self.assertFalse(g.freeze_check(n, ed=hot * 10, ee=hot * 10, top=hot * .5))
        self.assertFalse(g.freeze_check(n, ed=hot * 10, ee=0.0, top=hot * 4))
        inf = replay.ErleFreezeGate(self.x_cfg(dtfreeze_db=float('inf')), 2100)
        inf.erle_hat_db = 14.0
        self.assertFalse(inf.freeze_check(n, ed=hot * 10, ee=hot * 1e6, top=hot * 4))

    def test_x_equals_u_when_disabled(self):
        for kw in ({}, {'gain_db': -3.0}, {'caller_from': 300, 'caller_amp': 4000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 1100, 'ms': 1800}):
            u = Recorder(replay.NoDuckPreArmGate(self.x_cfg(), 2100))
            x = Recorder(replay.ErleFreezeGate(self.x_cfg(dtfreeze_db=float('inf')), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(x, **kw)
            self.assertEqual(x.events, u.events, kw)
            self.assertEqual(x.gate.freeze_packets, 0)
            self.assertEqual(x.gate.w, u.gate.w)

    def test_x_freezes_only_after_arming(self):
        x = Recorder(replay.ErleFreezeGate(self.x_cfg(), 2100))
        self.nlms.synthetic(x, caller_from=1500, caller_amp=1100, ms=1800)
        self.assertTrue(x.gate.armed)
        self.assertGreater(x.gate.freeze_packets, 0)  # the quiet caller trips the freeze
        self.assertGreaterEqual(x.gate.freeze_first_ms, x.gate.armed_ms)

    def test_x_keeps_more_quiet_caller_in_the_residual(self):
        # quiet caller after convergence: with the freeze the canceller adapts less on the caller, so its own
        # ERLE monitor stays higher than U's at the end of the call
        u = Recorder(replay.NoDuckPreArmGate(self.x_cfg(prearm_rms=1e9), 2100))
        x = Recorder(replay.ErleFreezeGate(self.x_cfg(prearm_rms=1e9), 2100))
        self.nlms.synthetic(u, caller_from=1200, caller_amp=1100, ms=2000)
        self.nlms.synthetic(x, caller_from=1200, caller_amp=1100, ms=2000)
        self.assertLess(x.gate.updates, u.gate.updates)
        self.assertGreater(x.gate.erle_hat_db, u.gate.erle_hat_db)

    def test_x_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)  # bargein_replay/18 bytes unchanged
        proc, data = run_cli(*W19_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), W19_SHA256)  # bargein_replay/19 bytes unchanged
        proc, data = run_cli(*R15_ARGS)
        self.assertEqual(sha256(data).hexdigest(), R15_SHA256)

    def test_cli_arm_x_leaves_other_arms_unchanged(self):
        proc, base = run_cli(*U18_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli(*U18_ARGS, '--arm-x', '--dtfreeze-db', '6', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, x = json.loads(base), json.loads(data)
        self.assertEqual(x['harness'], 'bargein_replay/20')
        self.assertEqual(sorted(x['arms']), ['A', 'B', 'O', 'Q', 'S', 'U', 'X'])
        for arm in ('A', 'B', 'O', 'Q', 'S', 'U'):
            self.assertEqual(canonical(x['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(x['parameters'])
        self.assertEqual(params.pop('dtfreeze_db'), 6.0)
        self.assertEqual(params, b['parameters'])
        xa = x['arms']['X']
        self.assertEqual(xa['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('dtfreeze_packets', xa)
        self.assertIn('prearm_silent_windows', xa)
        # a threshold no packet reaches: arm X is arm U trial by trial (freeze count 0, dt fraction unchanged)
        proc, data = run_cli(*U18_ARGS, '--arm-x', '--dtfreeze-db', '100')
        arms = json.loads(data)['arms']
        xa, ua = dict(arms['X']), dict(arms['U'])
        self.assertEqual(xa.pop('dtfreeze_packets'), 0)
        self.assertEqual(canonical(xa), canonical(ua))
        for bad in ((*U18_ARGS, '--arm-x'), (*U18_ARGS, '--arm-x', '--dtfreeze-db', '0'),
                    (*U18_ARGS, '--arm-x', '--dtfreeze-db', '-3'), (*U18_ARGS, '--arm-x', '--dtfreeze-db', '101')):
            proc, _ = run_cli(*bad)
            self.assertNotEqual(proc.returncode, 0, bad)
        args = [a for a in U18_ARGS if a != '--arm-u']  # --arm-x needs --arm-u
        proc, _ = run_cli(*args, '--arm-x', '--dtfreeze-db', '6')
        self.assertNotEqual(proc.returncode, 0)

    def test_only_arms_u_x(self):
        proc, full = run_cli(*U18_ARGS, '--arm-x', '--dtfreeze-db', '6')
        proc, part = run_cli(*U18_ARGS, '--arm-x', '--dtfreeze-db', '6', '--only-arms', 'U,X')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        f, p = json.loads(full), json.loads(part)
        self.assertEqual(sorted(p['arms']), ['A', 'B', 'U', 'X'])
        for arm in ('A', 'B', 'U', 'X'):
            self.assertEqual(canonical(p['arms'][arm]), canonical(f['arms'][arm]), arm)
        self.assertEqual(p['parameters'], f['parameters'])

    def test_sweep_passes_arm_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '300', '--arm-s',
                            '--prearm-rms', '5000', '--arm-u', '--arm-x', '--dtfreeze-db', '6', '--only-arms', 'U,X',
                            '--out', tmp], check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/20')
            self.assertEqual(res['extra_args'][-6:], ['--arm-u', '--arm-x', '--dtfreeze-db', '6.0', '--only-arms', 'U,X'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'X'])


X20_ARGS = (*U18_ARGS, '--arm-x', '--dtfreeze-db', '8', '--only-arms', 'U,X')
X20_SHA256 = '2ece7ced45b7c7530eabb43e3cb1af63d6aafeb5d914921f0c54c20b2d995010'  # computed on the unchanged /20 harness


class ErleTwoCueGateTest(unittest.TestCase):
    """bargein_replay/21: opt-in arm Y, arm X with ONE extra condition on its freeze: the packet's capture energy must
    also exceed the canceller's own a-priori echo prediction (the current filter applied to the render, i.e. render
    level times the learned echo return) by more than twocue_db. An ERLE dip on echo alone (capture about equal to
    the predicted echo) no longer freezes adaptation. No speech, onset or commit rule changes."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def y_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'dtfreeze_db': 6.0, 'twocue_db': 1.0} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_TWOCUE, 'bargein_replay/21')
        self.assertEqual(replay.HARNESS_DTFREEZE, 'bargein_replay/20')

    def test_y_is_x_subclass_reading_one_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.ErleTwoCueGate, replay.ErleFreezeGate))
        tree = ast.parse(inspect.getsource(replay.ErleTwoCueGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'twocue_db'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn('decide', names)
        self.assertNotIn('step', names)
        self.assertNotIn('freeze_check', names)  # the ERLE cue is X's, unchanged

    def test_excess_check_rule(self):
        g = replay.ErleTwoCueGate(self.y_cfg(twocue_db=1.0), 2100)
        # capture 2 dB over the predicted echo: passes; 0.5 dB over: no; under: no; no prediction at all: passes
        self.assertTrue(g.excess_check(ed=10 ** .2, ey=1.0))
        self.assertFalse(g.excess_check(ed=10 ** .05, ey=1.0))
        self.assertFalse(g.excess_check(ed=0.5, ey=1.0))
        self.assertTrue(g.excess_check(ed=1.0, ey=0.0))

    def test_y_equals_x_with_permissive_excess_and_u_with_strict(self):
        for kw in ({}, {'caller_from': 1500, 'caller_amp': 1100, 'ms': 1800},
                   {'caller_from': 1200, 'caller_amp': 1100, 'ms': 2000}):
            x = Recorder(replay.ErleFreezeGate(self.y_cfg(), 2100))
            y = Recorder(replay.ErleTwoCueGate(self.y_cfg(twocue_db=-100.0), 2100))
            self.nlms.synthetic(x, **kw)
            self.nlms.synthetic(y, **kw)
            self.assertEqual(y.events, x.events, kw)
            self.assertEqual(y.gate.w, x.gate.w)
            self.assertEqual(y.gate.freeze_packets, x.gate.freeze_packets)
            self.assertEqual(y.gate.twocue_vetoed, 0)
            u = Recorder(replay.NoDuckPreArmGate(self.y_cfg(), 2100))
            y = Recorder(replay.ErleTwoCueGate(self.y_cfg(twocue_db=100.0), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(y, **kw)
            self.assertEqual(y.events, u.events, kw)
            self.assertEqual(y.gate.w, u.gate.w)
            self.assertEqual(y.gate.freeze_packets, 0)

    def test_y_vetoes_some_x_freezes_and_keeps_caller_freezes(self):
        x = Recorder(replay.ErleFreezeGate(self.y_cfg(), 2100))
        y = Recorder(replay.ErleTwoCueGate(self.y_cfg(), 2100))
        self.nlms.synthetic(x, caller_from=1500, caller_amp=1100, ms=1800)
        self.nlms.synthetic(y, caller_from=1500, caller_amp=1100, ms=1800)
        self.assertGreater(y.gate.freeze_packets, 0)  # the quiet caller still trips both cues
        self.assertGreaterEqual(y.gate.freeze_first_ms, y.gate.armed_ms)
        self.assertLessEqual(y.gate.freeze_packets, x.gate.freeze_packets)

    def test_y_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)
        proc, data = run_cli(*X20_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), X20_SHA256)  # bargein_replay/20 bytes unchanged

    def test_cli_arm_y_leaves_other_arms_unchanged(self):
        proc, base = run_cli(*X20_ARGS[:-2])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli(*X20_ARGS[:-2], '--arm-y', '--twocue-db', '1.5', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, y = json.loads(base), json.loads(data)
        self.assertEqual(y['harness'], 'bargein_replay/21')
        self.assertEqual(sorted(y['arms']), ['A', 'B', 'O', 'Q', 'S', 'U', 'X', 'Y'])
        for arm in ('A', 'B', 'O', 'Q', 'S', 'U', 'X'):
            self.assertEqual(canonical(y['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(y['parameters'])
        self.assertEqual(params.pop('twocue_db'), 1.5)
        self.assertEqual(params, b['parameters'])
        ya = y['arms']['Y']
        self.assertEqual(ya['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('dtfreeze_packets', ya)
        self.assertIn('twocue_vetoed_packets', ya)
        self.assertIn('prearm_silent_windows', ya)
        # permissive excess margin: Y is X trial for trial (veto count 0)
        proc, data = run_cli(*X20_ARGS[:-2], '--arm-y', '--twocue-db', '-100', '--only-arms', 'X,Y')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        ya = dict(arms['Y'])
        self.assertEqual(ya.pop('twocue_vetoed_packets'), 0)
        self.assertEqual(canonical(ya), canonical(arms['X']))
        for bad in ((*X20_ARGS[:-2], '--arm-y'), (*X20_ARGS[:-2], '--arm-y', '--twocue-db', '101'),
                    (*X20_ARGS[:-2], '--arm-y', '--twocue-db', '-101'),
                    (*U18_ARGS, '--arm-y', '--twocue-db', '1')):  # --arm-y needs --arm-x
            proc, _ = run_cli(*bad)
            self.assertNotEqual(proc.returncode, 0, bad)

    def test_only_arms_u_y(self):
        proc, full = run_cli(*X20_ARGS[:-2], '--arm-y', '--twocue-db', '1.5')
        proc, part = run_cli(*X20_ARGS[:-2], '--arm-y', '--twocue-db', '1.5', '--only-arms', 'U,Y')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        f, p = json.loads(full), json.loads(part)
        self.assertEqual(sorted(p['arms']), ['A', 'B', 'U', 'Y'])
        for arm in ('A', 'B', 'U', 'Y'):
            self.assertEqual(canonical(p['arms'][arm]), canonical(f['arms'][arm]), arm)
        self.assertEqual(p['parameters'], f['parameters'])

    def test_sweep_passes_arm_y(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded', '--rhos',
                            'none', '--energy-k-db', '-12', '--energy-min-speech-ms', '30',
                            '--arm-o', '--resid-threshold', '1360', '--guard-packets', '5', '--guard-margin-db', '3',
                            '--arm-q', '--duck-commit-ms', '100', '--duck-window-ms', '300', '--arm-s',
                            '--prearm-rms', '5000', '--arm-u', '--arm-x', '--dtfreeze-db', '8', '--arm-y',
                            '--twocue-db', '1.5', '--only-arms', 'U,Y', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/21')
            self.assertEqual(res['extra_args'][-9:], ['--arm-u', '--arm-x', '--dtfreeze-db', '8.0', '--arm-y',
                                                      '--twocue-db', '1.5', '--only-arms', 'U,Y'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'Y'])


Y21_ARGS = (*X20_ARGS[:-2], '--arm-y', '--twocue-db', '1.5', '--only-arms', 'U,Y')
Y21_SHA256 = 'a7ac3b36805722f4765ed64bea24ca7ecbcea658c5568a7eb28117915ccb5a47'  # computed on the unchanged /21 harness
# bundled multi-speaker set (LibriSpeech dev-clean SPEAKERS.TXT sex column); none is 2277, the default clips' speaker
MULTI_SPEAKERS = {'1272': 'M', '174': 'M', '2803': 'M', '84': 'F', '1462': 'F', '5338': 'F'}
QU_MULTI_ARGS = ('--trials', '20', '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded',
                 '--clip-set', 'multi', '--energy-k-db=-12', '--energy-min-speech-ms', '30', '--resid-threshold',
                 '1360', '--arm-o', '--guard-packets', '5', '--arm-q', '--duck-window-ms', '300', '--arm-s',
                 '--prearm-rms', '5000', '--arm-u', '--only-arms', 'Q,U', '--caller-level-db-range=-10,0')


class MultiSpeakerClipSetTest(unittest.TestCase):
    """bargein_replay/22: opt-in --clip-set multi (needs --bot-render recorded). Caller and bot clips come from a
    bundled set of 0.85 s LibriSpeech dev-clean excerpts of six other speakers (three male, three female) in
    fixtures/bargein/multi/, and the bot clip is always a different SPEAKER from the caller clip (the default set
    has one speaker, so its bot clip is only a different clip). No gate, arm or constant changes."""

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_SPEAKERS, 'bargein_replay/22')

    def test_multi_set_loads_six_speakers_both_sexes(self):
        paths = sorted(replay.ASSETS_MULTI.glob('*.wav'))
        speakers = {replay.speaker_of(p.name) for p in paths}
        self.assertEqual(speakers, set(MULTI_SPEAKERS))
        self.assertGreaterEqual(len(speakers), 4)
        self.assertNotIn('2277', speakers)
        self.assertEqual(set(MULTI_SPEAKERS.values()), {'M', 'F'})
        self.assertLess(sum(p.stat().st_size for p in paths), 1_000_000)
        for p in paths:
            with replay.wave.open(str(p), 'rb') as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                                 (16000, 1, 2, 13600), p.name)
            samples = replay.clip(p)
            self.assertEqual(max(map(abs, samples)), 17500)  # peak-normalized like the default clips
            # onset- and level-matched to the 2277 clips: speech within the first 20 ms, normalized RMS >= 3645
            # and >= 71 % of frames >= 1360 RMS (checked with 1 % slack for the harness's integer rounding)
            self.assertGreaterEqual(sum(v * v for v in samples) / len(samples), (0.99 * 3645) ** 2, p.name)
            frames = [samples[i:i + 160] for i in range(0, len(samples) - 159, 160)]
            hot = [sum(v * v for v in f) >= 160 * 1360 ** 2 for f in frames]
            self.assertGreaterEqual(sum(hot), 0.70 * len(frames), p.name)
            self.assertTrue(hot[0] or hot[1], p.name)
        readme = (replay.ASSETS / 'README.md').read_text()
        for p in paths:
            self.assertIn(p.name, readme)
        # the default glob is not recursive: the default set still has exactly the two 2277 clips
        self.assertEqual(sorted(p.name for p in replay.ASSETS.glob('*.wav')),
                         ['2277-149896-0005.wav', '2277-149896-0026.wav'])

    def test_bot_speaker_never_caller_speaker(self):
        clips = {p.name: replay.clip(p) for p in sorted(replay.ASSETS_MULTI.glob('*.wav'))}
        for seed in (39, 34900, 34901):
            trials = list(replay.stimuli(200, seed, 'residual', 'recorded', clip_set='multi'))
            valid = [t for t in trials if t['kind'] == 'valid']
            self.assertEqual(len(valid), 140)
            for t in trials:
                self.assertIn(t['bot_clip'], clips)
                self.assertEqual(t['bot_speaker'], replay.speaker_of(t['bot_clip']))
                source, off = clips[t['bot_clip']], t['bot_offset']
                self.assertEqual(list(t['bot'][:800]), [source[(off + i) % len(source)] for i in range(800)])
                if t['kind'] == 'valid':
                    self.assertEqual(clips[t['mic_clip']], t['mic'])
                    self.assertEqual(t['mic_speaker'], replay.speaker_of(t['mic_clip']))
                    self.assertNotEqual(t['bot_speaker'], t['mic_speaker'])
                else:
                    self.assertIsNone(t['mic_clip'])
                    self.assertIsNone(t['mic_speaker'])
            self.assertGreaterEqual(len({t['mic_speaker'] for t in valid}), 4)
            self.assertGreaterEqual(len({t['bot_speaker'] for t in trials}), 4)
        with self.assertRaises(AssertionError):
            names = ['84-1-1.wav', '84-1-2.wav']
            replay.multi_speaker_bot(39, 0, [clips['84-121123-0018.wav']] * 2, names, exclude='84')
        with self.assertRaises(ValueError):
            list(replay.stimuli(10, 39, 'residual', 'periodic', clip_set='multi'))

    def test_default_set_unchanged(self):
        plain = list(replay.stimuli(60, 39, 'residual', 'recorded'))
        single = list(replay.stimuli(60, 39, 'residual', 'recorded', clip_set='single'))
        self.assertEqual([(t['mic_clip'], t['bot_clip'], t['bot_offset'], t['bot'], t['mic']) for t in plain],
                         [(t['mic_clip'], t['bot_clip'], t['bot_offset'], t['bot'], t['mic']) for t in single])
        self.assertNotIn('bot_speaker', plain[0])
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)
        proc, data = run_cli(*Y21_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), Y21_SHA256)  # bargein_replay/21 bytes unchanged
        proc, data = run_cli(*Y21_ARGS, '--clip-set', 'single')
        self.assertEqual(sha256(data).hexdigest(), Y21_SHA256)  # explicit default == no flag

    def test_cli_multi_marks_v22_aa_and_speakers(self):
        proc, data = run_cli('--seed', '34900', *QU_MULTI_ARGS, '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        d = json.loads(data)
        self.assertEqual(d['harness'], 'bargein_replay/22')
        self.assertEqual(d['parameters']['clip_set'], 'multi')
        self.assertEqual(sorted(d['arms']), ['A', 'B', 'Q', 'U'])
        for arm in ('Q', 'U'):
            for row in d['arms'][arm]['trials']:
                br = row['bot_render']
                self.assertEqual(br['bot_speaker'], br['clip'].split('-')[0])
                if row['kind'] == 'valid':
                    self.assertNotEqual(br['bot_speaker'], br['mic_speaker'])
        proc2, again = run_cli('--seed', '34900', *QU_MULTI_ARGS)
        self.assertEqual(sha256(again).hexdigest(), sha256(data).hexdigest())
        # without --clip-set the same flags give the old harness id and no clip_set parameter
        args = list(QU_MULTI_ARGS)
        del args[7:9]
        proc, old = run_cli('--seed', '34900', *args)
        o = json.loads(old)
        self.assertEqual(o['harness'], 'bargein_replay/18')
        self.assertNotIn('clip_set', o['parameters'])
        self.assertNotIn('bot_speaker', o['arms']['U']['trials'][0]['bot_render'])
        proc, _ = run_cli('--seed', '39', '--trials', '10', '--echo-model', 'residual', '--clip-set', 'multi')
        self.assertNotEqual(proc.returncode, 0)  # multi needs --bot-render recorded

    def test_sweep_passes_clip_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded',
                            '--clip-set', 'multi', '--rhos', 'none', '--energy-k-db', '-12',
                            '--energy-min-speech-ms', '30', '--arm-o', '--resid-threshold', '1360',
                            '--guard-packets', '5', '--guard-margin-db', '3', '--arm-q', '--duck-commit-ms', '100',
                            '--duck-window-ms', '300', '--arm-s', '--prearm-rms', '5000', '--arm-u',
                            '--only-arms', 'Q,U', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/22')
            self.assertEqual(res['extra_args'][:7], ['--echo-model', 'residual', '--echo-continuous', '--bot-render',
                                                     'recorded', '--clip-set', 'multi'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'Q', 'U'])


QU22_SHA256 = '199306aced73147891fd9df92e2f62c164f687169c762c062cc4d0092d83e5c9'  # seed 34900 QU_MULTI_ARGS, unchanged /22
Z23_FLAGS = ('--arm-z', '--floor-rms', '900', '--floor-k-db=9')


class FloorRelativeGateTest(unittest.TestCase):
    """bargein_replay/23: opt-in arm Z, arm U plus ONE onset/level rule. Once the residual gate is armed it keeps a
    running residual floor (EWMA 0.1, dB domain, of the residual level of armed packets it did NOT call speech).
    An armed packet that U does not call speech is speech when its residual RMS is >= floor_rms AND its level is
    >= floor_k_db above that floor. Same speech flag feeds the onset run and the commit count (no other change).
    floor_rms == resid_threshold: arm U exactly."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def z_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'floor_rms': 700.0, 'floor_k_db': 9.0} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_FLOOR, 'bargein_replay/23')
        self.assertEqual(replay.HARNESS_SPEAKERS, 'bargein_replay/22')

    def test_z_is_u_subclass_reading_two_new_constants(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.FloorRelativeGate, replay.NoDuckPreArmGate))
        tree = ast.parse(inspect.getsource(replay.FloorRelativeGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'floor_rms', 'floor_k_db'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {'caller_gain_db', 'erl_db', 'gain', 'taps', 'mic'}, node.attr)
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn('step', names)  # duck/commit/window logic is U's

    def test_decide_rule(self):
        n = 40
        g = replay.FloorRelativeGate(self.z_cfg(), 2100)
        g.armed, g.delay_ms, g.erle_hat_db = True, 20, 10.0
        # first armed non-speech packet seeds the floor (RMS 100 -> 40 dB)
        self.assertFalse(g.decide(n, n * 100.0 ** 2, 1.0, 0.0, 0.0))
        self.assertAlmostEqual(g.floor_db, 40.0)
        # 800 RMS: >= floor_rms 700 and 18 dB over the floor -> speech by the floor rule; floor not updated
        g.speech = False
        self.assertTrue(g.decide(n, n * 800.0 ** 2, 1.0, 0.0, 0.0))
        self.assertTrue(g.speech)
        self.assertEqual(g.floor_packets, 1)
        self.assertAlmostEqual(g.floor_db, 40.0)
        # 650 RMS: under floor_rms -> not speech; floor moves 10 % toward 56.26 dB
        self.assertFalse(g.decide(n, n * 650.0 ** 2, 1.0, 0.0, 0.0))
        self.assertAlmostEqual(g.floor_db, 40.0 + 0.1 * (20 * replay.log10(650.0) - 40.0))
        # U speech (>= resid_threshold) stays speech and is not counted as a floor packet
        self.assertTrue(g.decide(n, n * 1400.0 ** 2, 1.0, 0.0, 0.0))
        self.assertEqual(g.floor_packets, 1)
        # high floor: 800 RMS within floor_k_db of a 61 dB floor -> not speech
        g.floor_db = 61.0
        self.assertFalse(g.decide(n, n * 800.0 ** 2, 1.0, 0.0, 0.0))
        # unarmed: U's rule only, floor untouched
        h = replay.FloorRelativeGate(self.z_cfg(), 2100)
        self.assertFalse(h.decide(n, n * 1000.0 ** 2, 1.0, 0.0, 0.0))
        self.assertIsNone(h.floor_db)
        self.assertEqual(h.floor_packets, 0)

    def test_z_equals_u_when_floor_rms_is_resid_threshold(self):
        for kw in ({}, {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800},
                   {'caller_from': 1200, 'caller_amp': 3000, 'ms': 2000}):
            u = Recorder(replay.NoDuckPreArmGate(self.z_cfg(), 2100))
            z = Recorder(replay.FloorRelativeGate(self.z_cfg(floor_rms=1360.0), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(z, **kw)
            self.assertEqual(z.events, u.events, kw)
            self.assertEqual(z.gate.w, u.gate.w)
            self.assertEqual(z.gate.floor_packets, 0)

    def test_z_commits_a_quiet_caller_u_never_hears_at_a_duck_cost(self):
        cfg_z = self.z_cfg(floor_rms=900.0, floor_k_db=6.0)
        u = Recorder(replay.NoDuckPreArmGate(self.z_cfg(), 2100))
        z = Recorder(replay.FloorRelativeGate(cfg_z, 2100))
        self.nlms.synthetic(u, caller_from=1500, caller_amp=1000, ms=1800)
        self.nlms.synthetic(z, caller_from=1500, caller_amp=1000, ms=1800)
        self.assertEqual(u.events, [])  # residual RMS < resid_threshold: U never starts an onset
        self.assertIn('commit', [e for _, e in z.events])
        self.assertGreaterEqual([t for t, e in z.events if e == 'commit'][0], 1500 + 100)
        self.assertGreater(z.gate.floor_packets, 0)
        # no caller: Z never commits, but the lower onset level costs an echo-only duck that lapses (the price the
        # duck-FA guard measures)
        z0 = Recorder(replay.FloorRelativeGate(cfg_z, 2100))
        self.nlms.synthetic(z0)
        self.assertNotIn('commit', [e for _, e in z0.events])
        self.assertEqual([e for _, e in z0.events], ['duck', 'unduck'])
        # the frozen sweep setting (1100 RMS, 12 dB) adds no event on this synthetic, with or without the caller
        for kw in ({}, {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800}):
            zf = Recorder(replay.FloorRelativeGate(self.z_cfg(floor_rms=1100.0, floor_k_db=12.0), 2100))
            self.nlms.synthetic(zf, **kw)
            self.assertEqual(zf.events, [], kw)

    def test_z_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)
        proc, data = run_cli(*Y21_ARGS)
        self.assertEqual(sha256(data).hexdigest(), Y21_SHA256)
        proc, data = run_cli('--seed', '34900', *QU_MULTI_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), QU22_SHA256)  # bargein_replay/22 multi bytes unchanged

    def test_cli_arm_z_leaves_other_arms_unchanged(self):
        base_args = (*QU_MULTI_ARGS[:-3], '--caller-level-db-range=-10,0')  # drop --only-arms Q,U
        proc, base = run_cli('--seed', '34900', *base_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli('--seed', '34900', *base_args, *Z23_FLAGS, '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, z = json.loads(base), json.loads(data)
        self.assertEqual(z['harness'], 'bargein_replay/23')
        self.assertEqual(sorted(z['arms']), ['A', 'B', 'O', 'Q', 'S', 'U', 'Z'])
        for arm in ('A', 'B', 'O', 'Q', 'S', 'U'):
            self.assertEqual(canonical(z['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(z['parameters'])
        self.assertEqual(params.pop('floor_rms'), 900.0)
        self.assertEqual(params.pop('floor_k_db'), 9.0)
        self.assertEqual(params, b['parameters'])
        za = z['arms']['Z']
        self.assertEqual(za['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('floor_speech_packets', za)
        self.assertIn('prearm_silent_windows', za)
        # floor_rms == resid_threshold: Z is U trial for trial
        proc, data = run_cli('--seed', '34900', *base_args, '--arm-z', '--floor-rms', '1360', '--floor-k-db=9',
                             '--only-arms', 'U,Z')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        za = dict(arms['Z'])
        self.assertEqual(za.pop('floor_speech_packets'), 0)
        self.assertEqual(canonical(za), canonical(arms['U']))
        for bad in ((*base_args, '--arm-z'), (*base_args, '--arm-z', '--floor-rms', '1400', '--floor-k-db=9'),
                    (*base_args, '--arm-z', '--floor-rms', '0', '--floor-k-db=9'),
                    (*base_args, '--arm-z', '--floor-rms', '900', '--floor-k-db=0'),
                    (*base_args, '--arm-z', '--floor-rms', '900'),
                    ('--trials', '10', '--echo-model', 'residual', '--arm-z', '--floor-rms', '900',
                     '--floor-k-db=9')):  # --arm-z needs --arm-u
            proc, _ = run_cli('--seed', '39', *bad)
            self.assertNotEqual(proc.returncode, 0, bad)

    def test_only_arms_u_z(self):
        base_args = (*QU_MULTI_ARGS[:-3], '--caller-level-db-range=-10,0')
        proc, full = run_cli('--seed', '34900', *base_args, *Z23_FLAGS)
        proc, part = run_cli('--seed', '34900', *base_args, *Z23_FLAGS, '--only-arms', 'U,Z')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        f, p = json.loads(full), json.loads(part)
        self.assertEqual(sorted(p['arms']), ['A', 'B', 'U', 'Z'])
        for arm in ('A', 'B', 'U', 'Z'):
            self.assertEqual(canonical(p['arms'][arm]), canonical(f['arms'][arm]), arm)
        self.assertEqual(p['parameters'], f['parameters'])

    def test_sweep_passes_arm_z(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded',
                            '--clip-set', 'multi', '--rhos', 'none', '--energy-k-db', '-12',
                            '--energy-min-speech-ms', '30', '--arm-o', '--resid-threshold', '1360',
                            '--guard-packets', '5', '--guard-margin-db', '3', '--arm-q', '--duck-commit-ms', '100',
                            '--duck-window-ms', '300', '--arm-s', '--prearm-rms', '5000', '--arm-u', '--arm-z',
                            '--floor-rms', '900', '--floor-k-db', '9', '--only-arms', 'U,Z', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/23')
            self.assertEqual(res['extra_args'][-7:], ['--arm-u', '--arm-z', '--floor-rms', '900.0',
                                                      '--floor-k-db=9.0', '--only-arms', 'U,Z'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'Z'])


Z23_PIN_ARGS = (*QU_MULTI_ARGS[:-3], '--caller-level-db-range=-10,0', *Z23_FLAGS, '--only-arms', 'U,Z')
Z23_SHA256 = 'ba43ca1c18dcb764d5ffb7433aed82a9e2b067f9f3901f348fdeb8f67b035a0c'  # seed 34900, computed on unchanged /23
Z24_FLAGS = ('--arm-z2', '--floor-min-run-ms', '60')


class FloorRunGateTest(unittest.TestCase):
    """bargein_replay/24: opt-in arm Z2, arm Z whose floor-only packets (speech by Z's floor-relative rule but not
    by U's rule) need a longer consecutive speech run (floor_min_run_ms) before they count for the onset or the
    commit. Packets U calls speech keep U's rule (onset_ms run, commit count). floor_min_run_ms 0: arm Z exactly;
    floor_rms == resid_threshold: arm U exactly."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def z_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'floor_rms': 900.0, 'floor_k_db': 6.0, 'floor_min_run_ms': 60} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_FLOORRUN, 'bargein_replay/24')
        self.assertEqual(replay.HARNESS_FLOOR, 'bargein_replay/23')

    def test_z2_is_z_subclass_reading_one_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.FloorRunGate, replay.FloorRelativeGate))
        tree = ast.parse(inspect.getsource(replay.FloorRunGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'floor_min_run_ms'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {'caller_gain_db', 'erl_db', 'gain', 'taps', 'mic'}, node.attr)

    def drive(self, gate, flags, t0=1000):
        """Feed step() armed packets: 'U' = U speech, 'F' = floor-only speech, '.' = not speech."""
        gate.armed = True
        events = []
        for i, f in enumerate(flags):
            gate.floor_only = f == 'F'
            e = gate.step(t0 + 10 * i, f != '.')
            if e:
                events.append((10 * i, e))
        return events

    def test_step_rule(self):
        def mk(m=60):
            return replay.FloorRunGate(self.z_cfg(floor_min_run_ms=m), 2100)
        # 50 ms of floor-only speech: Z ducks at 30 ms, Z2 (60 ms) does not
        self.assertEqual(self.drive(mk(0), 'FFFFF.'), [(20, 'duck')])
        self.assertEqual(self.drive(mk(), 'FFFFF.'), [])
        # a sustained floor-only run ducks once it reaches 60 ms, with the whole run as commit evidence
        g = mk()
        self.assertEqual(self.drive(g, 'FFFFFF'), [(50, 'duck')])
        self.assertEqual(g.acc, 60)
        # U speech keeps U's 30 ms onset run (floor packets before it count, as in Z)
        self.assertEqual(self.drive(mk(), 'UUU'), [(20, 'duck')])
        self.assertEqual(self.drive(mk(), 'FFUUU'), [(40, 'duck')])
        self.assertEqual(self.drive(mk(), 'FUU'), [])  # U run 20 ms, whole run 30 ms < 60: no onset
        # ducked: a floor-only packet adds to the commit count only inside a >= 60 ms speech run
        g = mk()
        self.drive(g, 'UUU')
        acc = g.acc
        self.drive(g, '.F.F', t0=1100)
        self.assertEqual(g.acc, acc)
        self.assertGreater(g.floor_held, 0)
        g = mk()
        # run >= 60 ms from the 6th packet on: floor packets count again, 30 + 7 x 10 = 100 ms commits
        self.assertEqual(self.drive(g, 'UUU' + 'F' * 9), [(20, 'duck'), (110, 'commit')])

    def test_z2_equals_z_at_zero_and_u_at_resid_threshold(self):
        for kw in ({}, {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800, 'caller_to': 1560},
                   {'caller_from': 1200, 'caller_amp': 3000, 'ms': 2000}):
            z = Recorder(replay.FloorRelativeGate(self.z_cfg(), 2100))
            z2 = Recorder(replay.FloorRunGate(self.z_cfg(floor_min_run_ms=0), 2100))
            self.nlms.synthetic(z, **kw)
            self.nlms.synthetic(z2, **kw)
            self.assertEqual(z2.events, z.events, kw)
            self.assertEqual(z2.gate.floor_packets, z.gate.floor_packets)
            self.assertEqual(z2.gate.floor_held, 0)
            u = Recorder(replay.NoDuckPreArmGate(self.z_cfg(), 2100))
            z2u = Recorder(replay.FloorRunGate(self.z_cfg(floor_rms=1360.0), 2100))
            self.nlms.synthetic(u, **kw)
            self.nlms.synthetic(z2u, **kw)
            self.assertEqual(z2u.events, u.events, kw)
            self.assertEqual(z2u.gate.floor_packets, 0)

    def test_z2_keeps_the_sustained_quiet_caller_and_drops_the_echo_duck(self):
        z = Recorder(replay.FloorRelativeGate(self.z_cfg(), 2100))
        z2 = Recorder(replay.FloorRunGate(self.z_cfg(floor_min_run_ms=60), 2100))
        self.nlms.synthetic(z, caller_from=1500, caller_amp=1000, ms=1800)
        self.nlms.synthetic(z2, caller_from=1500, caller_amp=1000, ms=1800)
        # quiet caller (floor-only packets FFFFFF.FF.FF..): Z commits; Z2 ducks on the first 60 ms run, later than
        # Z's 30 ms duck, and its gapped floor packets after that are held (the price on S1 the dev grid measures)
        self.assertEqual([e for _, e in z.events][-2:], ['duck', 'commit'])
        z_duck = [t for t, e in z.events if e == 'duck'][-1]
        self.assertEqual([e for _, e in z2.events], ['duck'])
        self.assertEqual(z2.events[0][0], z_duck + 30)
        self.assertGreater(z2.gate.floor_held, 0)
        # no caller: Z's echo-only duck (a short floor-only run) is gone
        z0 = Recorder(replay.FloorRelativeGate(self.z_cfg(), 2100))
        z20 = Recorder(replay.FloorRunGate(self.z_cfg(floor_min_run_ms=60), 2100))
        self.nlms.synthetic(z0)
        self.nlms.synthetic(z20)
        self.assertEqual([e for _, e in z0.events], ['duck', 'unduck'])
        self.assertEqual(z20.events, [])

    def test_z2_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)
        proc, data = run_cli('--seed', '34900', *QU_MULTI_ARGS)
        self.assertEqual(sha256(data).hexdigest(), QU22_SHA256)
        proc, data = run_cli('--seed', '34900', *Z23_PIN_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), Z23_SHA256)  # bargein_replay/23 arm Z bytes unchanged

    def test_cli_arm_z2_leaves_other_arms_unchanged(self):
        base_args = (*QU_MULTI_ARGS[:-3], '--caller-level-db-range=-10,0', *Z23_FLAGS)
        proc, base = run_cli('--seed', '34900', *base_args, '--only-arms', 'U,Z')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli('--seed', '34900', *base_args, *Z24_FLAGS, '--only-arms', 'U,Z,Z2', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, z = json.loads(base), json.loads(data)
        self.assertEqual(z['harness'], 'bargein_replay/24')
        self.assertEqual(sorted(z['arms']), ['A', 'B', 'U', 'Z', 'Z2'])
        for arm in ('A', 'B', 'U', 'Z'):
            self.assertEqual(canonical(z['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(z['parameters'])
        self.assertEqual(params.pop('floor_min_run_ms'), 60)
        self.assertEqual(params, b['parameters'])
        za = z['arms']['Z2']
        self.assertEqual(za['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('floor_speech_packets', za)
        self.assertIn('floor_held_packets', za)
        # floor_min_run_ms 0: Z2 is Z trial for trial
        proc, data = run_cli('--seed', '34900', *base_args, '--arm-z2', '--floor-min-run-ms', '0',
                             '--only-arms', 'Z,Z2')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        za = dict(arms['Z2'])
        self.assertEqual(za.pop('floor_held_packets'), 0)
        self.assertEqual(canonical(za), canonical(arms['Z']))
        for bad in ((*base_args, '--arm-z2'), (*base_args, '--arm-z2', '--floor-min-run-ms', '-10'),
                    (*base_args, '--arm-z2', '--floor-min-run-ms', '1010'),
                    (*base_args, '--arm-z2', '--floor-min-run-ms', '55'),
                    (*QU_MULTI_ARGS[:-3], *Z24_FLAGS)):  # --arm-z2 needs --arm-z
            proc, _ = run_cli('--seed', '39', '--trials', '5', *bad)
            self.assertNotEqual(proc.returncode, 0, bad)

    def test_sweep_passes_arm_z2(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded',
                            '--clip-set', 'multi', '--rhos', 'none', '--energy-k-db', '-12',
                            '--energy-min-speech-ms', '30', '--arm-o', '--resid-threshold', '1360',
                            '--guard-packets', '5', '--guard-margin-db', '3', '--arm-q', '--duck-commit-ms', '100',
                            '--duck-window-ms', '300', '--arm-s', '--prearm-rms', '5000', '--arm-u', '--arm-z',
                            '--floor-rms', '1100', '--floor-k-db', '12', '--arm-z2', '--floor-min-run-ms', '60',
                            '--only-arms', 'U,Z2', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/24')
            self.assertEqual(res['extra_args'][-10:], ['--arm-u', '--arm-z', '--floor-rms', '1100.0',
                                                       '--floor-k-db=12.0', '--arm-z2', '--floor-min-run-ms', '60',
                                                       '--only-arms', 'U,Z2'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'Z2'])


UC_FLAGS = ('--arm-uc', '--rho-veto', '0.7')


class CoherenceVetoGateTest(unittest.TestCase):
    """bargein_replay/25: opt-in arm UC, arm U plus one veto: an armed packet U calls speech is not speech when the
    coherence rho between the canceller residual e and the canceller's echo estimate y_hat (20 ms, max over lags
    +-8 samples at 4 kHz) is >= rho_veto. Never adds speech, never acts before arming. rho_veto > 1: arm U."""

    @classmethod
    def setUpClass(cls):
        cls.nlms = NlmsGateTest('test_harness_id')
        cls.nlms.setUpClass()

    def uc_cfg(self, **overrides):
        return self.nlms.f_cfg(**({'resid_threshold': 1360.0, 'resid_arm_db': 6.0, 'resid_nlp_db': 3.0,
                                   'guard_erle_db': 6.0, 'guard_margin_db': 3.0, 'guard_packets': 5,
                                   'duck_commit_ms': 100, 'duck_window_ms': 300, 'prearm_rms': 5000.0,
                                   'rho_veto': 0.7} | overrides))

    def test_harness_id(self):
        self.assertEqual(replay.HARNESS_RHOVETO, 'bargein_replay/25')
        self.assertEqual(replay.HARNESS_FLOORRUN, 'bargein_replay/24')

    def test_uc_is_u_subclass_reading_one_new_constant(self):
        import ast
        import inspect
        self.assertTrue(issubclass(replay.CoherenceVetoGate, replay.NoDuckPreArmGate))
        tree = ast.parse(inspect.getsource(replay.CoherenceVetoGate))
        cfg_attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'cfg'}
        self.assertEqual(cfg_attrs, {'rho_veto'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ArmCTest.FORBIDDEN, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {'caller_gain_db', 'erl_db', 'gain', 'taps', 'mic'}, node.attr)

    def test_coherence_window(self):
        g = replay.CoherenceVetoGate(self.uc_cfg(), 2100)
        rng = replay.Random(7)
        y = [rng.gauss(0, 1000) for _ in range(400)]
        g.ybuf[:400] = y
        g.m1 = 400
        g.placed_at = None
        self.assertIsNone(g.coherence())  # filter not placed
        g.placed_at = 0
        g.ebuf[:400] = [0.5 * v for v in y]
        self.assertAlmostEqual(g.coherence(), 1.0, places=9)  # residual = scaled echo estimate
        g.ebuf[:400] = [-v for v in y[3:]] + [0.0] * 3  # lagged copy: found inside +-8 samples
        self.assertGreater(g.coherence(), 0.95)
        g.ebuf[:400] = [rng.gauss(0, 1000) for _ in range(400)]  # independent "caller"
        self.assertLess(g.coherence(), 0.5)
        g.placed_at = 330  # window [320, 400) not fully placed
        self.assertIsNone(g.coherence())
        g.placed_at = 0
        g.ebuf[:400] = [0.0] * 400  # zero norm
        self.assertIsNone(g.coherence())

    def test_veto_off_is_u_and_veto_only_removes_speech(self):
        class Speech(Recorder):
            def observe(self, t, packet):
                event = self.gate.observe(t, packet)
                if event:
                    self.events.append((t, event))
                self.speech.append((t, self.gate.speech, self.gate.armed))
                return False
        for kw in ({}, {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800},
                   {'caller_from': 1500, 'caller_amp': 1000, 'ms': 1800, 'caller_to': 1560},
                   {'caller_from': 1200, 'caller_amp': 3000, 'ms': 2000}):
            u = Speech(replay.NoDuckPreArmGate(self.uc_cfg(), 2100))
            off = Speech(replay.CoherenceVetoGate(self.uc_cfg(rho_veto=1.01), 2100))
            on = Speech(replay.CoherenceVetoGate(self.uc_cfg(rho_veto=0.3), 2100))
            for r in (u, off, on):
                r.speech = []
                self.nlms.synthetic(r, **kw)
            self.assertEqual(off.events, u.events, kw)
            self.assertEqual(off.speech, u.speech, kw)
            self.assertEqual(off.gate.rho_vetoes, 0)
            self.assertEqual(off.gate.w, u.gate.w)  # canceller arithmetic unchanged
            # with the veto on, every speech packet of UC is also U speech up to UC's first event (the veto never
            # adds speech); unarmed packets are never vetoed
            first = min([t for t, _ in on.events] + [t for t, _ in u.events] + [10 ** 9])
            for (t, s_u, a_u), (t2, s_on, a_on) in zip(u.speech, on.speech):
                self.assertEqual(t, t2)
                self.assertEqual(a_u, a_on)
                if t <= first:
                    self.assertLessEqual(s_on, s_u, (kw, t))
                    if not a_u:
                        self.assertEqual(s_on, s_u, (kw, t))

    def test_veto_drops_echo_speech_but_keeps_a_loud_caller(self):
        # echo-only synthetic at 0 dB echo gain: U calls armed echo packets speech; a strict veto removes them
        u = Recorder(replay.NoDuckPreArmGate(self.uc_cfg(), 2100))
        uc = Recorder(replay.CoherenceVetoGate(self.uc_cfg(rho_veto=0.5), 2100))
        self.nlms.synthetic(u, gain_db=0.0)
        self.nlms.synthetic(uc, gain_db=0.0)
        self.assertGreater(uc.gate.rho_vetoes, 0)
        self.assertLessEqual(len(uc.events), len(u.events))
        # a loud uncorrelated caller still commits with the veto on
        c = Recorder(replay.CoherenceVetoGate(self.uc_cfg(rho_veto=0.5), 2100))
        self.nlms.synthetic(c, caller_from=1200, caller_amp=3000, ms=2000)
        self.assertIn('commit', [e for _, e in c.events])

    def test_uc_off_keeps_old_bytes(self):
        proc, data = run_cli('--seed', '39')
        self.assertEqual(sha256(data).hexdigest(), REFERENCE_SHA256)
        proc, data = run_cli(*U18_ARGS)
        self.assertEqual(sha256(data).hexdigest(), U18_SHA256)
        proc, data = run_cli('--seed', '34900', *QU_MULTI_ARGS)
        self.assertEqual(sha256(data).hexdigest(), QU22_SHA256)
        proc, data = run_cli('--seed', '34900', *Z23_PIN_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha256(data).hexdigest(), Z23_SHA256)

    def test_cli_arm_uc_leaves_other_arms_unchanged(self):
        base_args = (*QU_MULTI_ARGS[:-3], '--caller-level-db-range=-10,0')
        proc, base = run_cli('--seed', '34900', *base_args, '--only-arms', 'U')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, data = run_cli('--seed', '34900', *base_args, *UC_FLAGS, '--only-arms', 'U,UC', '--aa')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)['aa_identical'], True)
        b, z = json.loads(base), json.loads(data)
        self.assertEqual(z['harness'], 'bargein_replay/25')
        self.assertEqual(sorted(z['arms']), ['A', 'B', 'U', 'UC'])
        for arm in ('A', 'B', 'U'):
            self.assertEqual(canonical(z['arms'][arm]), canonical(b['arms'][arm]), arm)
        params = dict(z['parameters'])
        self.assertEqual(params.pop('rho_veto'), 0.7)
        self.assertEqual(params, b['parameters'])
        za = z['arms']['UC']
        self.assertEqual(za['latency_definition'], 'time_to_ducked_or_silent')
        self.assertIn('rho_veto_packets', za)
        # rho_veto 1.01: UC is U trial for trial
        proc, data = run_cli('--seed', '34900', *base_args, '--arm-uc', '--rho-veto', '1.01', '--only-arms', 'U,UC')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arms = json.loads(data)['arms']
        za = dict(arms['UC'])
        self.assertEqual(za.pop('rho_veto_packets'), 0)
        self.assertEqual(canonical(za), canonical(arms['U']))
        for bad in ((*base_args, '--arm-uc'), (*base_args, '--arm-uc', '--rho-veto', '0'),
                    (*base_args, '--arm-uc', '--rho-veto', '1.5'),
                    tuple(a for a in base_args if a != '--arm-u') + UC_FLAGS):  # --arm-uc needs --arm-u
            proc, _ = run_cli('--seed', '39', '--trials', '5', *bad)
            self.assertNotEqual(proc.returncode, 0, bad)

    def test_sweep_passes_arm_uc(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-B', str(BENCH_DIR / 'bargein_sweep.py'), '--seeds', '39', '--trials', '10',
                            '--echo-model', 'residual', '--echo-continuous', '--bot-render', 'recorded',
                            '--clip-set', 'multi', '--rhos', 'none', '--energy-k-db', '-12',
                            '--energy-min-speech-ms', '30', '--arm-o', '--resid-threshold', '1360',
                            '--guard-packets', '5', '--guard-margin-db', '3', '--arm-q', '--duck-commit-ms', '100',
                            '--duck-window-ms', '300', '--arm-s', '--prearm-rms', '5000', '--arm-u', '--arm-uc',
                            '--rho-veto', '0.7', '--only-arms', 'U,UC', '--out', tmp],
                           check=True, capture_output=True, timeout=900)
            res = json.loads(Path(tmp, 'seed-39.json').read_text())
            self.assertEqual(res['harness'], 'bargein_replay/25')
            self.assertEqual(res['extra_args'][-6:], ['--arm-u', '--arm-uc', '--rho-veto', '0.7',
                                                      '--only-arms', 'U,UC'])
            self.assertEqual(sorted(res['arms']), ['A', 'B', 'U', 'UC'])


if __name__ == '__main__':
    unittest.main()
