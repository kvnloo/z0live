#!/usr/bin/env python3
"""EOS -> first audio of chained_engine.ChainedEngine (on_device slice) with REAL local stages. Bench only, no wiring.

Stages (all local, offline):
  STT   = MLX Whisper via bench/whisper_stt_helper.py (one long-lived helper under --helper-python)
  model = a private llama.cpp llama-server on an ephemeral 127.0.0.1 port (local_voice_replay.server), streamed
  TTS   = Lure MLX via bench/lure_tts_helper.py (LureTTS from local_voice_replay), --lure-first-chunk-frames 1 default
Inputs are synthetic utterance WAVs (16 kHz mono int16 + manifest.json with the spoken text), e.g. made once with
--make-utterances (Lure speaks fixed public questions).
Endpointer (OD-slice-2): each utterance plus trailing silence is streamed in 20 ms frames at real-time deadlines
through local_voice_replay.Endpointer (energy VAD, --vad-db, --endpointer-silence-ms; default 500 ms = the provider
server_vad silence window the deployed engines leave at default). EOS = the moment the endpointer finalizes the
utterance; speech_end = the wall time of the clip's last sample at or above the VAD threshold. Per turn (ms):
  endpointer_ms (speech_end -> EOS)
  eos_to_stt_ms, stt_to_first_token_ms, first_token_to_first_clause_ms, first_clause_to_first_audio_ms
  (these four sum to) eos_to_first_audio_ms; endpointer_ms + eos_to_first_audio_ms = speech_end_to_first_audio_ms
--endpointer-silence-ms 0 restores the OD-slice-1 offline EOS (whole clip handed over; no endpointer).
Design: arms A and A2 are the same config (A/A), interleaved over --runs runs (A,A2 / A2,A / ...); each pass plays
every utterance once in a seeded order. --gate CMD runs before every pass; a nonzero exit stops the bench (aborted).
"""
import argparse
import asyncio
from contextlib import AsyncExitStack, aclosing, suppress
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import struct
import subprocess
import sys
import time
import wave

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # chained_engine; local_voice_replay is imported lazily (script dir = bench/)
import chained_engine as ce  # noqa: E402

STAGES = ('eos_to_stt_ms', 'stt_to_first_token_ms', 'first_token_to_first_clause_ms', 'first_clause_to_first_audio_ms')
METRICS = ('endpointer_ms',) + STAGES + ('eos_to_first_audio_ms', 'speech_end_to_first_audio_ms')
RATE = 16000
FRAME = 320  # 20 ms at 16 kHz, the Endpointer's frame
VAD_DB = -43.0  # local_voice_replay --vad-db default
ENDPOINT_SILENCE_MS = 500  # provider server_vad default silence window (GPT-Live / Grok sessions set none)
QUESTIONS = (  # public, generic test prompts (no private content)
    'What is the capital of France?', 'How many legs does a spider have?', 'What color is the sky on a clear day?',
    'Who wrote Romeo and Juliet?', 'What is the boiling point of water in Celsius?', 'How many days are in a week?',
    'What is the largest planet in our solar system?', 'What do bees make?', 'How many minutes are in an hour?',
    'What is the opposite of hot?', 'Which ocean is the largest?', 'What gas do plants take in from the air?')


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def norm_words(text):
    return re.sub(r"[^a-z0-9 ]", '', (text or '').lower().replace('-', ' ')).split()


def as_bytes(tts):
    """Adapt a TTS stage that yields numpy PCM to the engine's bytes contract (empty chunks dropped)."""
    async def stage(clause):
        async with aclosing(tts(clause)) as stream:
            async for pcm in stream:
                data = pcm.tobytes() if hasattr(pcm, 'tobytes') else bytes(pcm)
                if data:
                    yield data
    return stage


def speech_end_sample(pcm16, db=VAD_DB):
    """Index of the last sample whose magnitude reaches the VAD threshold (the true end of speech), or None."""
    x = np.abs(np.frombuffer(pcm16, '<i2').astype(np.int32))
    voiced = np.nonzero(x >= 32768 * 10 ** (db / 20))[0]
    return int(voiced[-1]) if len(voiced) else None


def _endpointer(db, silence_ms):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import local_voice_replay as lvr
    return lvr.Endpointer(db, silence_ms)


def padded_frames(pcm16, silence_ms):
    """The utterance plus trailing silence (>= silence window + 300 ms, >= 1 s), cut into 20 ms frames."""
    x = np.frombuffer(pcm16, '<i2')
    tail = max(RATE, RATE * (silence_ms + 300) // 1000)
    x = np.concatenate([x, np.zeros(tail + (-(len(x) + tail)) % FRAME, '<i2')])
    return x.reshape(-1, FRAME)


def endpoint_offline(pcm16, db=VAD_DB, silence_ms=ENDPOINT_SILENCE_MS):
    """Sample clock only: (speech_end_s, eos_s, utterance bytes); eos_s = end of the frame that finalized."""
    ep, end = _endpointer(db, silence_ms), speech_end_sample(pcm16, db)
    end_s = None if end is None else (end + 1) / RATE
    for k, frame in enumerate(padded_frames(pcm16, silence_ms)):
        _, out = ep.feed(frame, (k + 1) * FRAME / RATE)
        if out is not None:
            return end_s, (k + 1) * FRAME / RATE, out.tobytes()
    return end_s, None, None


async def stream_endpoint(pcm16, db=VAD_DB, silence_ms=ENDPOINT_SILENCE_MS, clock=time.perf_counter_ns,
                          sleep=asyncio.sleep):
    """Real-time paced: frame k is fed at its end deadline. Returns (speech_end_ns, eos_ns, utterance bytes)."""
    ep, end = _endpointer(db, silence_ms), speech_end_sample(pcm16, db)
    t0 = clock()
    speech_end_ns = None if end is None else t0 + (end + 1) * 1_000_000_000 // RATE
    for k, frame in enumerate(padded_frames(pcm16, silence_ms)):
        due = t0 + (k + 1) * FRAME * 1_000_000_000 // RATE
        if (wait := due - clock()) > 0:
            await sleep(wait / 1e9)
        _, out = ep.feed(frame, (k + 1) * FRAME / RATE)
        if out is not None:
            return speech_end_ns, clock(), out.tobytes()
    return speech_end_ns, None, None


async def run_live_turn(stages, pcm, turn_id=0, db=VAD_DB, silence_ms=ENDPOINT_SILENCE_MS,
                        clock=time.perf_counter_ns, sleep=asyncio.sleep):
    """Endpointer, then one engine turn; adds endpointer_ms and speech_end_to_first_audio_ms to run_turn's row."""
    if not silence_ms:
        return await run_turn(stages, pcm, turn_id=turn_id, clock=clock)
    speech_end_ns, eos_ns, utt = await stream_endpoint(pcm, db, silence_ms, clock, sleep)
    if eos_ns is None or speech_end_ns is None:
        row = {'turn_id': turn_id, 'ok': False, 'error_stage': 'endpointer', 'error': 'no utterance finalized'}
        row.update({k: None for k in METRICS})
        return row
    if eos_ns < speech_end_ns:  # a mid-utterance pause >= the window finalized early: the turn would be split
        row = {'turn_id': turn_id, 'ok': False, 'error_stage': 'endpointer', 'error': 'split before speech end',
               'endpointer_declared_ms': (eos_ns - speech_end_ns) / 1e6, 'transcript': None, 'reply': None}
        row.update({k: None for k in METRICS})
        return row
    row = await run_turn(stages, utt, turn_id=turn_id, clock=clock)
    row['endpointer_declared_ms'] = (eos_ns - speech_end_ns) / 1e6
    if row['ok']:
        row['endpointer_ms'] = (row['eos_ns'] - speech_end_ns) / 1e6  # to the engine's own EOS mark
        row['speech_end_to_first_audio_ms'] = row['endpointer_ms'] + row['eos_to_first_audio_ms']
    return row


async def run_turn(stages, pcm, turn_id=0, clock=time.perf_counter_ns):
    """One ChainedEngine turn; returns a row with the four stage gaps and EOS -> first audio (ms)."""
    marks = {}

    def trace(event, **_):
        if event == 'chained.eos':
            marks['t0'] = clock()

    def emit(event):
        if event.get('type') == 'output_transcript.added' and 'clause' not in marks:
            marks['clause'], marks['clause_text'] = clock(), event['text']

    engine = ce.ChainedEngine(*stages, trace=trace, emit=emit, clock=clock)
    r = await engine.turn(pcm, turn_id=turn_id)
    ms = (lambda ns: None if ns is None else ns / 1e6)
    clause_ns = marks['clause'] - marks['t0'] if 'clause' in marks else None
    ok = r.ok and r.first_audio_ns is not None and clause_ns is not None
    row = {'turn_id': turn_id, 'ok': ok, 'error_stage': r.error_stage, 'error': r.error, 'transcript': r.text,
           'reply': r.reply, 'first_clause': marks.get('clause_text'), 'chunks': r.chunks, 'bytes': r.bytes,
           'done_ms': ms(r.done_ns)}
    if ok:
        row.update(eos_to_stt_ms=ms(r.stt_ns), stt_to_first_token_ms=ms(r.first_token_ns - r.stt_ns),
                   first_token_to_first_clause_ms=ms(clause_ns - r.first_token_ns),
                   first_clause_to_first_audio_ms=ms(r.first_audio_ns - clause_ns),
                   eos_to_first_audio_ms=ms(r.first_audio_ns), eos_ns=marks['t0'],
                   endpointer_ms=None, speech_end_to_first_audio_ms=None)
    else:
        row.update({k: None for k in METRICS})
    return row


def schedule(runs, n_utts, seed):
    """A/A plan: per run two passes (A then A2, swapped every other run), each a seeded order of all utterances."""
    rng, plan = random.Random(seed), []
    for run in range(1, runs + 1):
        for arm in (('A', 'A2') if run % 2 else ('A2', 'A')):
            order = list(range(n_utts))
            rng.shuffle(order)
            plan.append({'run': run, 'arm': arm, 'order': order})
    return plan


def window_arms(spec):
    """'500,300,200,500' -> [('W500', 500), ('W300', 300), ('W200', 200), ('W500b', 500)] (repeats get b, c, ...)."""
    arms, seen = [], {}
    for tok in (t.strip() for t in spec.split(',') if t.strip()):
        ms = int(tok)
        k = seen.get(ms, 0)
        seen[ms] = k + 1
        arms.append((f'W{ms}' + ('' if not k else chr(ord('a') + k)), ms))
    return arms


def schedule_windows(runs, n_utts, seed, arms):
    """Window sweep plan: per run one seeded utterance order; every utterance is played once per arm back to back,
    the arm order rotated by one each utterance so no arm always goes first. Returns one pass per run whose 'turns'
    are (utterance index, arm label, window ms)."""
    rng, plan, slot = random.Random(seed), [], 0
    for run in range(1, runs + 1):
        order = list(range(n_utts))
        rng.shuffle(order)
        turns = []
        for idx in order:
            turns += [(idx, *arms[(slot + j) % len(arms)]) for j in range(len(arms))]
            slot += 1
        plan.append({'run': run, 'arm': 'sweep', 'turns': turns})
    return plan


def _stats(rows):
    ok = [r for r in rows if r.get('ok')]
    out = {'n': len(ok), 'errors': len(rows) - len(ok),
           'premature_endpoints': sum(r.get('error') == 'split before speech end' for r in rows)}
    for k in METRICS:
        xs = [r[k] for r in ok if r.get(k) is not None]
        out[k] = {'p50': pct(xs, .5), 'p95': pct(xs, .95), 'min': min(xs, default=None), 'max': max(xs, default=None)}
    if any('stt_exact' in r for r in ok):
        out['stt_exact_rate'] = sum(bool(r.get('stt_exact')) for r in ok) / len(ok)
    return out


def summarize(rows):
    arms = {a: _stats([r for r in rows if r.get('arm') == a]) for a in sorted({r.get('arm') for r in rows})}
    pooled = _stats(rows)
    primary = ('speech_end_to_first_audio_ms' if pooled['speech_end_to_first_audio_ms']['p50'] is not None
               else 'eos_to_first_audio_ms')
    band = None
    pair = next((p for p in (('A', 'A2'), ('W500', 'W500b')) if set(p) <= arms.keys()), None)
    if pair and arms[pair[0]]['n'] and arms[pair[1]]['n']:
        a, b = arms[pair[0]][primary], arms[pair[1]][primary]
        band = {'p50': abs(a['p50'] - b['p50']), 'p95': abs(a['p95'] - b['p95'])}
    p50s = {k: pooled[k]['p50'] for k in ('endpointer_ms',) + STAGES if pooled[k]['p50'] is not None}
    e2a = pooled[primary]
    return {'primary': primary, 'arms': arms, 'pooled': pooled, 'aa_band_ms': band,
            'dominant_stage': max(p50s, key=p50s.get) if p50s else None,
            'bar': {'p50_le_500': e2a['p50'] is not None and e2a['p50'] <= 500,
                    'p95_le_1000': e2a['p95'] is not None and e2a['p95'] <= 1000}}


class HelperSTT:
    """STT through one long-lived helper process (bench/whisper_stt_helper.py protocol)."""
    def __init__(self, argv, log):
        self.argv, self.log, self.proc = [str(a) for a in argv], Path(log), None

    async def __aenter__(self):
        with self.log.open('ab') as stream:
            self.proc = await asyncio.create_subprocess_exec(*self.argv, stdin=subprocess.PIPE,
                                                             stdout=subprocess.PIPE, stderr=stream)
        try:
            ready = json.loads(await asyncio.wait_for(self._line(), 300))
            if ready.get('event') != 'ready':
                raise RuntimeError(f'STT helper did not get ready; see {self.log}')
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc):
        if self.proc and self.proc.returncode is None:
            with suppress(BrokenPipeError, ConnectionResetError):
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()

    async def _line(self):
        line = await self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f'STT helper exited; see {self.log}')
        return line

    async def stt(self, pcm: bytes) -> str:
        self.proc.stdin.write(struct.pack('<I', len(pcm) // 2) + pcm)
        await self.proc.stdin.drain()
        return json.loads(await self._line())['text'].strip()


def read_wav(path):
    with wave.open(str(path), 'rb') as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise ValueError(f'{path}: need 16 kHz mono int16')
        return w.readframes(w.getnframes())


def write_wav(path, pcm16):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        w.writeframes(pcm16.astype('<i2', copy=False).tobytes())


def lure_argv(a):
    return [a.helper_python, '-I', HERE / 'lure_tts_helper.py', '--model', a.lure_model, '--voice', a.lure_voice,
            '--voice-text', a.lure_voice_text, '--seed', str(a.lure_seed),
            '--first-chunk-frames', str(a.lure_first_chunk_frames)]


async def make_utterances(a):
    import local_voice_replay as lvr
    a.make_utterances.mkdir(parents=True, exist_ok=False)
    manifest = []
    async with lvr.LureTTS(lure_argv(a), a.make_utterances / 'lure_make.log') as lure:
        for i, text in enumerate(QUESTIONS):
            pcm48 = np.concatenate([x async for x in lure.tts(text)])
            name = f'u{i:02d}.wav'
            write_wav(a.make_utterances / name, pcm48[::3])  # 48 kHz playout -> 16 kHz, as local_voice_replay warms ASR
            manifest.append({'file': name, 'text': text, 'seconds': round(len(pcm48) / 48000, 3)})
    (a.make_utterances / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    return {'made': len(manifest), 'dir': a.make_utterances.name}


def gate_ok(cmd):
    return not cmd or subprocess.run(shlex.split(cmd), capture_output=True).returncode == 0


async def bench(a):
    import local_voice_replay as lvr
    from aiohttp import ClientSession, ClientTimeout
    manifest = json.loads((a.utterances / 'manifest.json').read_text())
    utts = [(m, read_wav(a.utterances / m['file'])) for m in manifest]
    arms = window_arms(a.arm_windows) if a.arm_windows else None
    plan = schedule_windows(a.runs, len(utts), a.seed, arms) if arms else schedule(a.runs, len(utts), a.seed)
    rows, aborted, t_start = [], None, time.monotonic()
    async with ClientSession(timeout=ClientTimeout(total=60), trust_env=False) as http, AsyncExitStack() as stack:
        stt = await stack.enter_async_context(HelperSTT(
            [a.helper_python, '-I', HERE / 'whisper_stt_helper.py', '--model', a.whisper_model], a.out / 'stt.log'))
        url = await stack.enter_async_context(lvr.server(a.llama_server, [
            '-m', a.gguf.expanduser().resolve(strict=True), '-c', '4096', '-ngl', '99', '-np', '1', '--jinja',
            '--no-webui', '--reasoning', 'off'], a.out / 'llm.log', 'listening on {url}'))
        lure = await stack.enter_async_context(lvr.LureTTS(lure_argv(a), a.out / 'lure.log'))

        async def model(text):
            msgs = [lvr.message('system', lvr.SYSTEM), lvr.message('user', text)]
            async with aclosing(lvr.deltas(http, url, msgs, temperature=0, max_tokens=96,
                                           chat_template_kwargs={'enable_thinking': False})) as stream:
                async for delta in stream:
                    if tok := delta.get('content'):
                        yield tok

        stages = (stt.stt, model, as_bytes(lure.tts))
        for i in range(a.warmup):
            row = await run_live_turn(stages, utts[i % len(utts)][1], turn_id=-1 - i, db=a.vad_db,
                                      silence_ms=arms[i % len(arms)][1] if arms else a.endpointer_silence_ms)
            print(json.dumps({'warmup': i, 'ok': row['ok'], 'e2a': row['eos_to_first_audio_ms'],
                              'se2a': row['speech_end_to_first_audio_ms']}), file=sys.stderr)
        turn = 0
        for p in plan:
            if not gate_ok(a.gate):
                aborted = f'gate before run {p["run"]} {p["arm"]}'
                break
            turns = p.get('turns') or [(idx, p['arm'], a.endpointer_silence_ms) for idx in p['order']]
            for idx, arm, window in turns:
                if arms and not gate_ok(a.gate):  # sweep: gate before every arm turn
                    aborted = f'gate before run {p["run"]} {arm} turn {turn}'
                    break
                m, pcm = utts[idx]
                row = await run_live_turn(stages, pcm, turn_id=turn, db=a.vad_db, silence_ms=window)
                turn += 1
                row.update(run=p['run'], arm=arm, window_ms=window, utt=m['file'], expected=m['text'],
                           stt_exact=norm_words(row['transcript']) == norm_words(m['text']),
                           load1=os.getloadavg()[0])
                rows.append(row)
                print(json.dumps({k: row[k] for k in ('run', 'arm', 'utt', 'ok', 'endpointer_ms',
                                                      'eos_to_first_audio_ms', 'speech_end_to_first_audio_ms')}),
                      file=sys.stderr, flush=True)
            if aborted:
                break
    return {'bench': 'od_slice_bench', 'aborted': aborted, 'wall_s': round(time.monotonic() - t_start, 2),
            'runs': a.runs, 'warmup': a.warmup, 'seed': a.seed, 'lure_first_chunk_frames': a.lure_first_chunk_frames,
            'vad_db': a.vad_db, 'endpointer_silence_ms': a.endpointer_silence_ms, 'arm_windows': arms,
            'n_utterances': len(utts), 'utterances_sha256': hashlib.sha256(
                b''.join(pcm for _, pcm in utts)).hexdigest(), 'summary': summarize(rows), 'rows': rows}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, help='new private receipt directory outside this worktree')
    ap.add_argument('--make-utterances', type=Path, help='only synthesize the test utterances into this new dir')
    ap.add_argument('--utterances', type=Path, help='dir with manifest.json + 16 kHz mono WAVs')
    ap.add_argument('--helper-python', default=os.environ.get('LURE_PYTHON', sys.executable),
                    help='python of a venv with mlx-audio + lure_tts (runs the STT and TTS helpers)')
    ap.add_argument('--whisper-model', type=Path, help='local Whisper MLX dir (weights + HF tokenizer files)')
    ap.add_argument('--gguf', type=Path, help='local GGUF lane model for llama-server')
    ap.add_argument('--llama-server', type=Path, default=Path(os.environ.get('LLAMA_SERVER', 'llama-server')))
    ap.add_argument('--lure-model', type=Path, required=True)
    ap.add_argument('--lure-voice', type=Path, required=True)
    ap.add_argument('--lure-voice-text', required=True)
    ap.add_argument('--lure-seed', type=int, default=1234)
    ap.add_argument('--lure-first-chunk-frames', type=int, default=1)
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--warmup', type=int, default=2)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--vad-db', type=float, default=VAD_DB, help='Endpointer energy threshold (dBFS)')
    ap.add_argument('--endpointer-silence-ms', type=int, default=ENDPOINT_SILENCE_MS,
                    help='trailing-silence window before EOS (multiple of 20 in 80..2000; 0 = offline EOS)')
    ap.add_argument('--arm-windows', default='',
                    help='window sweep, e.g. 500,300,200,500: one arm per window (repeats = A/A, labeled W500b), '
                         'interleaved per utterance; overrides --endpointer-silence-ms; default off')
    ap.add_argument('--gate', help='command run before every pass; nonzero exit aborts the bench')
    a = ap.parse_args(argv)
    if a.make_utterances is None and not (a.out and a.utterances and a.whisper_model and a.gguf):
        ap.error('bench needs --out, --utterances, --whisper-model and --gguf (or use --make-utterances)')
    if a.endpointer_silence_ms % 20 or not (a.endpointer_silence_ms == 0 or 80 <= a.endpointer_silence_ms <= 2000):
        ap.error('--endpointer-silence-ms must be 0 or a multiple of 20 in 80..2000')
    try:
        bad = [ms for _, ms in window_arms(a.arm_windows) if ms % 20 or not 80 <= ms <= 2000]
    except ValueError:
        bad = [a.arm_windows]
    if bad:
        ap.error('--arm-windows entries must be multiples of 20 in 80..2000')
    for name in ('out', 'make_utterances'):
        p = getattr(a, name)
        if p is not None:
            p = p.expanduser().resolve()
            if p.is_relative_to(HERE.parents[1]):
                ap.error(f'--{name.replace("_", "-")} must be outside the worktree')
            setattr(a, name, p)
    return a


def main():
    a = parse_args()
    os.umask(0o077)
    if a.make_utterances is not None:
        print(json.dumps(asyncio.run(make_utterances(a))))
        return 0
    a.out.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(bench(a))
    (a.out / 'raw.json').write_text(json.dumps(result, indent=1))
    (a.out / 'summary.json').write_text(json.dumps({k: v for k, v in result.items() if k != 'rows'}, indent=1))
    print(json.dumps(result['summary'], indent=1))
    return 1 if result['aborted'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
