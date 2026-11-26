#!/usr/bin/env python3
"""EOS -> first audio of chained_engine.ChainedEngine (on_device slice) with REAL local stages. Bench only, no wiring.

Stages (all local, offline):
  STT   = MLX Whisper via bench/whisper_stt_helper.py (one long-lived helper under --helper-python)
  model = a private llama.cpp llama-server on an ephemeral 127.0.0.1 port (local_voice_replay.server), streamed
  TTS   = Lure MLX via bench/lure_tts_helper.py (LureTTS from local_voice_replay), --lure-first-chunk-frames 1 default
Inputs are synthetic utterance WAVs (16 kHz mono int16 + manifest.json with the spoken text), e.g. made once with
--make-utterances (Lure speaks fixed public questions). EOS = the moment the whole utterance is handed to the engine
(offline STT; no endpointer hangover is included). Per turn (ms from EOS, perf_counter clock of the engine):
  eos_to_stt_ms, stt_to_first_token_ms, first_token_to_first_clause_ms, first_clause_to_first_audio_ms
  (these four sum to) eos_to_first_audio_ms
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
METRICS = STAGES + ('eos_to_first_audio_ms',)
RATE = 16000
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
                   eos_to_first_audio_ms=ms(r.first_audio_ns))
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


def _stats(rows):
    ok = [r for r in rows if r.get('ok')]
    out = {'n': len(ok), 'errors': len(rows) - len(ok)}
    for k in METRICS:
        xs = [r[k] for r in ok]
        out[k] = {'p50': pct(xs, .5), 'p95': pct(xs, .95), 'min': min(xs, default=None), 'max': max(xs, default=None)}
    if any('stt_exact' in r for r in ok):
        out['stt_exact_rate'] = sum(bool(r.get('stt_exact')) for r in ok) / len(ok)
    return out


def summarize(rows):
    arms = {a: _stats([r for r in rows if r.get('arm') == a]) for a in sorted({r.get('arm') for r in rows})}
    pooled = _stats(rows)
    band = None
    if {'A', 'A2'} <= arms.keys() and arms['A']['n'] and arms['A2']['n']:
        a, b = arms['A']['eos_to_first_audio_ms'], arms['A2']['eos_to_first_audio_ms']
        band = {'p50': abs(a['p50'] - b['p50']), 'p95': abs(a['p95'] - b['p95'])}
    p50s = {k: pooled[k]['p50'] for k in STAGES if pooled[k]['p50'] is not None}
    e2a = pooled['eos_to_first_audio_ms']
    return {'arms': arms, 'pooled': pooled, 'aa_band_ms': band,
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
    plan = schedule(a.runs, len(utts), a.seed)
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
            row = await run_turn(stages, utts[i % len(utts)][1], turn_id=-1 - i)
            print(json.dumps({'warmup': i, 'ok': row['ok'], 'e2a': row['eos_to_first_audio_ms']}), file=sys.stderr)
        turn = 0
        for p in plan:
            if not gate_ok(a.gate):
                aborted = f'gate before run {p["run"]} {p["arm"]}'
                break
            for idx in p['order']:
                m, pcm = utts[idx]
                row = await run_turn(stages, pcm, turn_id=turn)
                turn += 1
                row.update(run=p['run'], arm=p['arm'], utt=m['file'], expected=m['text'],
                           stt_exact=norm_words(row['transcript']) == norm_words(m['text']),
                           load1=os.getloadavg()[0])
                rows.append(row)
                print(json.dumps({k: row[k] for k in ('run', 'arm', 'utt', 'ok', 'eos_to_first_audio_ms')}),
                      file=sys.stderr, flush=True)
    return {'bench': 'od_slice_bench', 'aborted': aborted, 'wall_s': round(time.monotonic() - t_start, 2),
            'runs': a.runs, 'warmup': a.warmup, 'seed': a.seed, 'lure_first_chunk_frames': a.lure_first_chunk_frames,
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
    ap.add_argument('--gate', help='command run before every pass; nonzero exit aborts the bench')
    a = ap.parse_args(argv)
    if a.make_utterances is None and not (a.out and a.utterances and a.whisper_model and a.gguf):
        ap.error('bench needs --out, --utterances, --whisper-model and --gguf (or use --make-utterances)')
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
