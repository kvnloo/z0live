#!/usr/bin/env python3
"""Paced Dessa PCM replay through private Liquid and Gemma subprocesses, without WebRTC."""
import argparse
import asyncio
import base64
from collections import deque
from contextlib import aclosing, AsyncExitStack, asynccontextmanager, suppress
import io
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import wave

import numpy as np
from aiohttp import ClientError, ClientSession, ClientTimeout

import dessa_replay as dr
import dessa_timing as dt
from dessa_score import score_d

HERE = Path(__file__).resolve().parent
HOME = Path.home()
MODELS = HOME / '.local/share/z0live/models'
LIQUID = 'LFM2.5-Audio-1.5B-Q4_0.gguf'
RUNNER = 'llama-liquid-audio-macos-arm64'
RATE, PLAY_RATE, FRAME = 16000, 48000, 320
SYSTEM = ('You are on a voice call. Respond naturally in one short, complete spoken sentence. '
          'Answer general knowledge and reasoning questions directly. Use supplied context for personal facts. '
          'For unavailable personal or live information, or requests to take an external action, '
          'say only SKIP. You have no tools.')
DEFER = "I can't check that from here."


def message(role, content):
    return {'role': role, 'content': content}


@asynccontextmanager
async def server(binary, args, log, ready):
    binary = binary.expanduser().resolve(strict=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('LLAMA_', 'GGML_'))}
    with log.open('wb') as stream:
        proc = await asyncio.create_subprocess_exec(
            binary, '--host', '127.0.0.1', '--port', str(port), '--offline', *args,
            stdout=stream, stderr=stream, cwd=binary.parent, env=env)
    try:
        deadline = time.monotonic() + 90
        with log.open('rb') as reader:
            while proc.returncode is None and time.monotonic() < deadline:
                if any(ready.format(url=url).encode() in line for line in reader):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f'Model server did not start; see {log}')
        yield url
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()


async def deltas(http, url, messages, **options):
    async with http.post(url + '/v1/chat/completions', allow_redirects=False,
                         json=dict(messages=messages, stream=True, **options)) as response:
        if response.status != 200 or response.content_type != 'text/event-stream':
            raise RuntimeError(f'Unexpected model HTTP response: {response.status}')
        stop = False
        async for line in response.content:
            if not line.startswith(b'data:'):
                continue
            data = line[5:].strip()
            if data == b'[DONE]':
                if stop:
                    return
                break
            event = json.loads(data)
            if 'error' in event:
                raise RuntimeError('Model reported a stream error')
            choice = event['choices'][0]
            if choice['finish_reason'] is not None:
                if choice['finish_reason'] != 'stop':
                    raise RuntimeError(f'Incomplete generation: {choice["finish_reason"]}')
                stop = True
            yield choice['delta']
        raise RuntimeError('Model stream did not finish with stop and [DONE]')


def audio_message(pcm):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        w.writeframes(pcm.astype('<i2', copy=False).tobytes())
    return [{'type': 'input_audio', 'input_audio': {
        'format': 'wav', 'data': base64.b64encode(buf.getvalue()).decode()}}]


def play_pcm(pcm):
    """24 kHz float PCM -> 48 kHz int16 playout samples (sample repeat, as the Liquid path always did)."""
    return np.repeat((np.clip(pcm, -1, 1) * 32767).astype('<i2'), 2)


class UtteranceDump:
    """Opt-in per-utterance receipt for --lure-dump: one 24 kHz int16 WAV per Lure utterance plus a JSONL index row
    (id, tag, call, turn, input text, complete flag, sample count) so speech can be transcribed utterance by utterance.
    A barge-in keeps the samples received before the cancel and marks the row complete=false."""
    def __init__(self, root, tag):
        self.root, self.tag, self.call, self.turn = Path(root), tag, 'warmup', 0
        self.root.mkdir(parents=True, exist_ok=True)

    def start_call(self, call):
        self.call, self.turn = call, 0

    def write(self, text, chunks, complete):
        uid = f'{self.tag}__{self.call}__t{self.turn:02d}'
        pcm = np.concatenate(chunks) if chunks else np.zeros(0, '<f4')
        with wave.open(str(self.root / f'{uid}.wav'), 'wb') as w:
            w.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes())
        row = dict(id=uid, tag=self.tag, call=self.call, turn=self.turn, text=text, complete=complete,
                   samples=len(pcm))
        with (self.root / 'utterances.jsonl').open('a') as index:
            index.write(json.dumps(row) + '\n')
        self.turn += 1


class LureTTS:
    """Opt-in Lure TTS through one long-lived helper process (bench/lure_tts_helper.py in an MLX venv).

    Requests are JSON lines on stdin ({"text": ...}; {"cancel": true} stops the current utterance). Replies on stdout
    are frames of a little-endian uint32 sample count followed by that many float32 24 kHz samples; a zero count ends
    an utterance, and one zero count before any request means the helper finished loading and warming up.
    """
    def __init__(self, argv, log, dump=None):
        self.argv, self.log, self.dump = [str(a) for a in argv], Path(log), dump
        self.proc, self.pending = None, False

    async def __aenter__(self):
        with self.log.open('ab') as stream:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stream)
        try:
            await asyncio.wait_for(self._drain(), 300)
        except BaseException:
            await self.__aexit__(None, None, None)
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

    async def _frame(self):
        try:
            n, = struct.unpack('<I', await self.proc.stdout.readexactly(4))
            return np.frombuffer(await self.proc.stdout.readexactly(4 * n), '<f4') if n else None
        except asyncio.IncompleteReadError:
            raise RuntimeError(f'Lure helper exited; see {self.log}') from None

    async def _drain(self):
        while await self._frame() is not None:
            pass
        self.pending = False

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + '\n').encode())

    async def tts(self, text):
        if self.pending:  # the previous turn was cut off: discard what the helper sent after the cancel
            await self._drain()
        self.pending = True
        self._send({'text': text})
        done, heard = False, []
        try:
            while (pcm := await self._frame()) is not None:
                if not np.isfinite(pcm).all():
                    raise RuntimeError('Invalid Lure PCM')
                if len(pcm):
                    if self.dump is not None:
                        heard.append(pcm)
                    yield play_pcm(pcm)
            done, self.pending = True, False
        finally:
            if self.dump is not None:
                self.dump.write(text, heard, done)
            if not done and self.proc.returncode is None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self._send({'cancel': True})


def lure_helper_argv(args, first_chunk_frames=None):
    """Helper command line; first_chunk_frames overrides --lure-first-chunk-frames (one helper per interleaved arm)."""
    fc = args.lure_first_chunk_frames if first_chunk_frames is None else first_chunk_frames
    argv = [args.lure_python, '-I', HERE / 'lure_tts_helper.py', '--model', args.lure_model,
            '--voice', args.lure_voice, '--voice-text', args.lure_voice_text, '--seed', str(args.lure_seed),
            '--first-chunk-frames', str(fc)]
    return [str(a) for a in argv] + ([] if args.lure_snapshot else ['--no-snapshot'])


class Models:
    def __init__(self, http, asr_url, tts_url, gemma, lure=None):
        self.http, self.asr_url, self.tts_url, self.gemma, self.lure = http, asr_url, tts_url, gemma, lure

    async def text(self, url, messages, marks, name, **options):
        chunks = []
        async with aclosing(deltas(self.http, url, messages, **options)) as stream:
            async for delta in stream:
                if delta.get('audio_chunk'):
                    raise RuntimeError('Audio from text-only request')
                if text := delta.get('content'):
                    marks.setdefault(name + '_first', time.monotonic())
                    chunks.append(text)
        marks[name + '_done'] = time.monotonic()
        return ''.join(chunks).strip()

    async def asr(self, pcm, marks):
        return await self.text(self.asr_url, [message('system', 'Perform ASR.'),
                               message('user', audio_message(pcm))], marks, 'asr',
                               reset_context=True, max_tokens=512)

    async def reply(self, messages, marks):
        return await self.text(self.gemma, messages, marks, 'gemma', temperature=0, max_tokens=96,
                               chat_template_kwargs={'enable_thinking': False})

    async def tts(self, text):
        if self.lure is not None:
            async with aclosing(self.lure.tts(text)) as stream:
                async for samples in stream:
                    yield samples
            return
        msgs = [message('system', 'Perform TTS. Use the US male voice.'), message('user', text)]
        async with aclosing(deltas(self.http, self.tts_url, msgs, reset_context=True, max_tokens=152)) as stream:
            async for delta in stream:
                if chunk := delta.get('audio_chunk'):
                    if chunk['sample_rate'] != 24000:
                        raise RuntimeError('Unexpected Liquid PCM sample rate')
                    pcm = np.frombuffer(base64.b64decode(chunk['data'], validate=True), '<f4')
                    if not np.isfinite(pcm).all():
                        raise RuntimeError('Invalid Liquid PCM')
                    if len(pcm):
                        yield play_pcm(pcm)


class Endpointer:
    """20 ms mono 16 kHz frames -> confirmed onset or finalized utterance."""
    def __init__(self, db, silence_ms):
        self.threshold = (32768 * 10 ** (db / 20)) ** 2
        self.silence = silence_ms // 20
        self.pre = deque(maxlen=8)
        self.active = []
        self.run = self.quiet = self.voiced = 0
        self.last_voice = 0.0

    def feed(self, frame, at):
        speech = np.mean(frame.astype(np.float32) ** 2) >= self.threshold
        if speech:
            self.last_voice = at
        if not self.active:
            self.pre.append(frame)
            self.run = self.run + 1 if speech else 0
            if self.run >= 3:
                self.active = list(self.pre)
                self.voiced = self.run
                return True, None
            return False, None
        self.active.append(frame)
        self.quiet = 0 if speech else self.quiet + 1
        self.voiced += int(speech)
        if self.quiet < self.silence and len(self.active) < 750:
            return False, None
        end = len(self.active) - max(0, self.quiet - 8)
        pcm = np.concatenate(self.active[:end]) if self.voiced >= 6 else None
        self.active, self.run, self.quiet = [], 0, 0
        self.pre.clear()
        return False, pcm


class Playout:
    """One 48 kHz sample clock for both tracks. Only queued, unheard samples can be cut."""
    def __init__(self, start):
        self.start = start
        self.pcm = bytearray()
        self.end = 0

    def position(self, now):
        return max(0, round((now - self.start) * PLAY_RATE))

    def append(self, samples, now):
        pos = max(self.end, self.position(now))
        self.pcm.extend(bytes(2 * (pos - self.end)))
        self.pcm.extend(samples.astype('<i2', copy=False).tobytes())
        self.end = pos + len(samples)
        return pos

    def cut(self, now):
        self.end = min(self.end, self.position(now))
        del self.pcm[self.end * 2:]


class TurnLog:
    """Opt-in per-turn wall-clock and load receipt (--interleave-arms only; default rows stay byte-identical).

    Adds to each turn row: wall = Unix epochs (s) of the call start, the last voiced input frame (end of speech), the
    endpoint (asr_start) and the first voiced playout sample; load1 = host 1-min load average at the endpoint; cpu_pct =
    this bench process's CPU % (all threads, not the TTS helper) from call start to the endpoint.
    """
    WALL = ('last_voice', 'asr_start', 'voiced_playout')

    def __init__(self, clock=time.time, load=os.getloadavg, cpu=time.process_time):
        self.clock, self.load, self.cpu = clock, load, cpu
        self.start = self.offset = self.cpu0 = None

    def begin(self, start, now=None):
        self.start, self.offset = start, (now or self.clock)() - time.monotonic()
        self.cpu0 = self.cpu()

    def eos(self, row, at):
        row['load1'] = round(float(self.load()[0]), 2)
        span = at - self.start
        row['cpu_pct'] = round(100 * (self.cpu() - self.cpu0) / span, 1) if span > 0 else None

    def finish(self, row):
        """Call while marks are still monotonic (before conversation() rebases them to the call start)."""
        marks = row['marks']
        row['wall'] = {'call_start': round(self.start + self.offset, 3),
                       **{k: round(marks[k] + self.offset, 3) for k in self.WALL if k in marks}}


async def conversation(models, pcm, db, silence_ms, context='', turns=(), drain=False, turn_log=None):
    history = list(turns)
    rows, task = [], None
    start = time.monotonic()
    if turn_log is not None:
        turn_log.begin(start)
    playback = Playout(start)
    endpoint = Endpointer(db, silence_ms)
    frames = np.frombuffer(pcm, '<i2')[::3]

    async def answer(utterance, row):
        marks = row['marks']
        try:
            transcript = row['transcript'] = await models.asr(utterance, marks)
            if not transcript:
                row['status'] = 'empty_asr'
                return
            user = message('user', transcript)
            marks['gemma_start'] = time.monotonic()
            text = row['reply'] = await models.reply(
                [message('system', SYSTEM + context), *history[-6:], user], marks)
            if not text:
                raise RuntimeError('Empty Gemma reply')
            spoken = DEFER if re.search(r'\bSKIP\b', text) else text
            marks['tts_start'] = time.monotonic()
            async with aclosing(models.tts(spoken)) as stream:
                async for samples in stream:
                    now = time.monotonic()
                    marks.setdefault('first_pcm', now)
                    pos = playback.append(samples, now)
                    if 'voiced_playout' not in marks:
                        voiced = np.flatnonzero(np.abs(samples.astype(np.int32)) >= 104)  # -50 dBFS
                        if len(voiced):
                            marks['voiced_playout'] = start + (pos + int(voiced[0])) / PLAY_RATE
            marks['tts_done'] = time.monotonic()
            if 'voiced_playout' not in marks:
                raise RuntimeError('No voiced PCM from Liquid TTS')
            await asyncio.sleep(max(0, start + playback.end / PLAY_RATE - time.monotonic()))
            history.extend([user, message('assistant', spoken)])
            row['status'] = 'played'
        except asyncio.CancelledError:
            raise
        except (ClientError, TimeoutError, ValueError, RuntimeError, KeyError, IndexError) as exc:
            row.update(status='error', error=str(exc))

    async def interrupt(reason):
        if task is not None and not task.done():
            playback.cut(time.monotonic())
            rows[-1]['status'] = reason
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    try:
        for i in range(0, len(frames), FRAME):
            at = start + (i + FRAME) / RATE  # frame is seen only after its samples arrive
            await asyncio.sleep(max(0, at - time.monotonic()))
            frame = frames[i:i + FRAME]
            onset, utterance = endpoint.feed(frame, at)
            if onset:
                await interrupt('barge_in')
            if utterance is not None:
                row = {'marks': {'last_voice': endpoint.last_voice, 'asr_start': time.monotonic()}}
                if turn_log is not None:
                    turn_log.eos(row, row['marks']['asr_start'])
                rows.append(row)
                task = asyncio.create_task(answer(utterance, row))
        if drain and task is not None:
            await task
    finally:
        await interrupt('clip_end')
    for row in rows:
        if turn_log is not None:
            turn_log.finish(row)
        row['marks'] = {k: round(v - start, 4) for k, v in row['marks'].items()}
    size = max(len(pcm), len(playback.pcm)) if drain else len(pcm)
    return bytes(playback.pcm[:size]).ljust(size, b'\0'), rows


async def replay(models, args):
    per, offsets, bc, minutes, errors = {}, [], 0, 0.0, []
    for name in args.calls.split(','):
        try:
            if models.lure is not None and models.lure.dump is not None:
                models.lure.dump.start_call(name)
            pcm = dr.replay_input(name, args.clip)
            ai, rows = await conversation(models, pcm, args.vad_db, args.silence_ms)
            (args.out / f'{name}-local.json').write_text(json.dumps(rows, indent=2))
            result = dr.score_audio(name, pcm, ai, args.out)
            offsets.extend(result.pop('offsets'))
            bc += result['backchannels']
            minutes += result['minutes']
            statuses = {s: sum(r['status'] == s for r in rows) for s in sorted({r['status'] for r in rows})}
            per[name] = {**result, 'turns': statuses}
            if not result['ai_talk_s'] or statuses.get('error'):
                errors.append(f'{name}: no scored audio or model error; see per-call receipt')
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            errors.append(f'{name}: {exc}')
    ai = dt.summary(offsets, bc, minutes)
    if ai:
        ai['D'] = round(score_d(ai['median_s'], ai['over_1s_pct'], ai['backchannels_per_min']), 3)
    else:
        errors.append('No scored floor-takes')
    return dict(engine='local', calls_done=len(per), errors=errors, minutes=round(minutes, 2),
                ai=ai, per_call=per, out=str(args.out), **lure_extra(args))


def arm_orders(names):
    """Rotations of the given arm order plus their mirrors (duplicates dropped). Across one set every arm sits in every
    slot equally often and every ordered neighbour pair appears in both directions; for 3 arms it is all 6 orders."""
    base, orders = tuple(names), []
    rotations = [base[i:] + base[:i] for i in range(len(base))]
    for order in rotations + [tuple(reversed(r)) for r in rotations]:
        if order not in orders:
            orders.append(order)
    return orders


def arm_schedule(names, units, seed):
    """Per-unit arm order: consecutive blocks each use every arm_orders() order once, block order shuffled by seed."""
    rng, out = random.Random(seed), []
    while len(out) < units:
        block = arm_orders(names)
        rng.shuffle(block)
        out.extend(block)
    return out[:units]


async def replay_interleaved(pipes, args):
    """--interleave-arms: for every (round, call) unit, replay that call once per arm back to back, arms in the
    arm_schedule() order, each arm on its own warm Lure helper (pipes: arm -> Models). Receipts per unit and arm go to
    OUT/r<round>/<arm>/; every turn row carries arm, fc, round, call, slot plus TurnLog epochs and load."""
    names, fcs = [n for n, _ in args.interleave_arms], dict(args.interleave_arms)
    units = [(r, name) for r in range(1, args.interleave_rounds + 1) for name in args.calls.split(',')]
    acc = {arm: dict(offsets=[], bc=0, minutes=0.0, calls=0) for arm in names}
    per, errors, schedule = {}, [], []
    for (r, name), order in zip(units, arm_schedule(names, len(units), args.interleave_seed)):
        schedule.append(dict(round=r, call=name, order=list(order)))
        try:
            pcm = dr.replay_input(name, args.clip)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            errors.append(f'r{r}/{name}: {exc}')
            continue
        for slot, arm in enumerate(order):
            key = f'r{r}/{arm}/{name}'
            try:
                models = pipes[arm]
                if models.lure is not None and models.lure.dump is not None:
                    models.lure.dump.start_call(f'r{r}-{name}')
                ai, rows = await conversation(models, pcm, args.vad_db, args.silence_ms, turn_log=TurnLog())
                for row in rows:
                    row.update(arm=arm, fc=fcs[arm], round=r, call=name, slot=slot)
                out = args.out / f'r{r}' / arm
                out.mkdir(parents=True, exist_ok=True)
                (out / f'{name}-local.json').write_text(json.dumps(rows, indent=2))
                result = dr.score_audio(name, pcm, ai, out)
                a = acc[arm]
                a['offsets'].extend(result.pop('offsets'))
                a['bc'] += result['backchannels']
                a['minutes'] += result['minutes']
                a['calls'] += 1
                statuses = {s: sum(r_['status'] == s for r_ in rows) for s in sorted({r_['status'] for r_ in rows})}
                per[key] = {**result, 'turns': statuses, 'arm': arm, 'round': r, 'call': name, 'slot': slot}
                if not result['ai_talk_s'] or statuses.get('error'):
                    errors.append(f'{key}: no scored audio or model error; see per-call receipt')
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                errors.append(f'{key}: {exc}')
    per_arm = {}
    for arm in names:
        a = acc[arm]
        ai = dt.summary(a['offsets'], a['bc'], a['minutes'])
        if ai:
            ai['D'] = round(score_d(ai['median_s'], ai['over_1s_pct'], ai['backchannels_per_min']), 3)
        else:
            errors.append(f'{arm}: No scored floor-takes')
        per_arm[arm] = dict(calls_done=a['calls'], minutes=round(a['minutes'], 2), ai=ai)
    return dict(engine='local', calls_done=len(per), errors=errors, per_arm=per_arm, schedule=schedule,
                per_call=per, out=str(args.out), **lure_extra(args))


def lure_extra(args):
    """Extra result keys for --tts lure; the default (Liquid) output keys stay exactly as before."""
    if args.tts == 'liquid':
        return {}
    extra = dict(tts='lure', lure_first_chunk_frames=args.lure_first_chunk_frames, lure_snapshot=args.lure_snapshot)
    if args.lure_dump is not None:
        extra['lure_dump'] = str(args.lure_dump)
    if args.interleave_arms:  # per-arm frames replace the single --lure-first-chunk-frames value
        del extra['lure_first_chunk_frames']
        extra.update(interleave_arms=dict(args.interleave_arms), interleave_rounds=args.interleave_rounds,
                     interleave_seed=args.interleave_seed)
    return extra


def grade(case, text, audible=False):
    if case.get('skip'):
        return bool(re.fullmatch(r'i can(?:not|[’\']t) check that from here[.!]?', text.strip(), re.I)
                    if audible else re.search(r'\bSKIP\b', text))
    return not re.search(r'\bSKIP\b', text) and bool(re.search(case['answer'], text, re.I))


async def spoken_suite(models, args):
    suite = json.loads((HERE / 'slm-v1.json').read_text())
    rows = []
    for i, case in enumerate(suite['cases']):
        if args.limit and i >= args.limit:
            break
        name = case['id']
        source = args.out / f'{name}-say.aiff'
        voice_name = 'Samantha' if i % 2 else 'Alex'
        try:
            subprocess.run(['say', '-v', voice_name, '-o', str(source), '--', case['ask']], check=True, timeout=8)
        except subprocess.TimeoutExpired:
            # macOS say can finish writing its AIFF but hang while exiting.
            pass
        except subprocess.CalledProcessError:
            source.unlink(missing_ok=True)
        try:
            voice = dt.pcm(source)
            if not np.any(voice):
                raise ValueError('No speech in fixture')
        except (subprocess.CalledProcessError, ValueError):
            rows.append(dict(case=name, kind=case['kind'], turns=[], heard='', error='No usable say audio',
                             text_correct=False, spoken_correct=False))
            continue
        tail = max(1.0, args.silence_ms / 1000 + 0.3)
        pcm = bytes(PLAY_RATE * 2 // 2) + np.repeat((voice * 32768).astype('<i2'), 3).tobytes() \
            + bytes(round(tail * PLAY_RATE) * 2)
        ai, turns = await conversation(models, pcm, args.vad_db, args.silence_ms,
                                       '\n\n' + suite['brief'], [message(*t) for t in case.get('turns', [])], drain=True)
        dr.save(args.out / f'{name}-human.wav', pcm.ljust(len(ai), b'\0'))
        reply_path = args.out / f'{name}-ai.wav'
        dr.save(reply_path, ai)
        heard, error = '', None
        row = turns[-1] if len(turns) == 1 else {}
        if row.get('status') == 'played':
            onset = max(0, int((row['marks']['first_pcm'] - 0.02) * RATE))
            try:
                reply = dt.pcm(reply_path)[onset:]
                heard = await models.asr((reply * 32768).astype('<i2'), {})
                if not heard:
                    error = 'Empty playback transcript'
            except (ClientError, TimeoutError, ValueError, RuntimeError, KeyError, IndexError) as exc:
                error = str(exc)
        else:
            error = 'Expected one fully played turn'
        rows.append(dict(case=name, kind=case['kind'], turns=turns, heard=heard, error=error,
                         text_correct=grade(case, row.get('reply', '')), spoken_correct=grade(case, heard, audible=True)))
        (args.out / 'slm-v1-rows.json').write_text(json.dumps(rows, indent=2))
    return dict(suite=suite['suite'], cases=len(rows), errors=[r['case'] for r in rows if r['error']],
                text_correct=sum(r['text_correct'] for r in rows), spoken_correct=sum(r['spoken_correct'] for r in rows),
                by_kind={k: [sum(r['spoken_correct'] for r in rows if r['kind'] == k),
                             sum(r['kind'] == k for r in rows)] for k in sorted({r['kind'] for r in rows})},
                out=str(args.out))


async def run(args):
    parent = asyncio.current_task()
    assert parent is not None
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, parent.cancel)
    try:
        async with ClientSession(timeout=ClientTimeout(total=45), trust_env=False) as http, AsyncExitStack() as stack:
            models = args.liquid_dir.expanduser().resolve()
            liquid = models / RUNNER / RUNNER / 'llama-liquid-audio-server'
            liquid_args = ['-c', '2048', '-ngl', 'all', '-s', '42', '-m', models / LIQUID,
                           '-mm', models / ('mmproj-' + LIQUID), '-mv', models / ('vocoder-' + LIQUID),
                           '--tts-speaker-file', models / ('tokenizer-' + LIQUID)]
            modes = ('asr', 'tts') if args.tts == 'liquid' else ('asr',)  # Lure replaces only Liquid's TTS role
            asr, *tts = [await stack.enter_async_context(server(liquid, liquid_args, args.out / f'{mode}.log',
                                                               'Server ready at {url}')) for mode in modes]
            gemma = await stack.enter_async_context(server(args.llama_server,
                ['-m', args.gemma.expanduser().resolve(strict=True), '-c', '4096', '-ngl', '99', '-np', '1', '--jinja',
                 '--no-webui', '--reasoning', 'off'], args.out / 'gemma.log', 'listening on {url}'))
            lure, arms = None, {}
            if args.interleave_arms:  # one warm Lure helper per arm, all alive for the whole run
                for arm, fc in args.interleave_arms:
                    dump = (UtteranceDump(args.lure_dump / arm, f'{args.lure_dump_tag}_{arm}')
                            if args.lure_dump is not None else None)
                    arm_lure = await stack.enter_async_context(
                        LureTTS(lure_helper_argv(args, fc), args.out / f'lure-{arm}.log', dump))
                    arms[arm] = Models(http, asr, None, gemma, arm_lure)
                for arm, other in list(arms.items())[1:]:  # the first arm is warmed below with the ASR/reply check
                    if not [samples async for samples in other.tts('What is the capital of France?')]:
                        raise RuntimeError(f'Warm-up produced no audio ({arm})')
            elif args.tts == 'lure':
                dump = UtteranceDump(args.lure_dump, args.lure_dump_tag) if args.lure_dump is not None else None
                lure = await stack.enter_async_context(LureTTS(lure_helper_argv(args), args.out / 'lure.log', dump))
            pipe = next(iter(arms.values())) if arms else Models(http, asr, tts[0] if tts else None, gemma, lure)
            warm = [samples async for samples in pipe.tts('What is the capital of France?')]
            if not warm:
                raise RuntimeError('Warm-up produced no audio')
            text = await pipe.asr(np.concatenate(warm)[::3], {})
            if not text or not await pipe.reply([message('system', SYSTEM), message('user', text)], {}):
                raise RuntimeError('Warm-up produced no text')
            if arms:
                return await replay_interleaved(arms, args)
            return await (spoken_suite(pipe, args) if args.slm else replay(pipe, args))
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True, help='new private receipt directory outside this worktree')
    ap.add_argument('--calls', default=','.join(dr.DEFAULT_CALLS))
    ap.add_argument('--clip', default=60., type=float)
    ap.add_argument('--vad-db', default=-43., type=float)
    ap.add_argument('--silence-ms', default=440, type=int)
    ap.add_argument('--liquid-dir', default=MODELS / 'lfm25-audio', type=Path)
    ap.add_argument('--gemma', default=MODELS / 'gemma4-e4b.gguf', type=Path)
    ap.add_argument('--llama-server', default=Path(os.environ.get('LLAMA_SERVER', HOME /
                    '.local/opt/llama.cpp/b12527/llama-b12527/llama-server')), type=Path)
    ap.add_argument('--slm', action='store_true', help='speak and grade the frozen slm-v1 suite instead')
    ap.add_argument('--limit', type=int, help='limit spoken cases for a smoke run')
    ap.add_argument('--tts', choices=('liquid', 'lure'), default='liquid',
                    help='TTS engine; lure runs bench/lure_tts_helper.py under --lure-python (ASR stays Liquid)')
    ap.add_argument('--lure-python', default=os.environ.get('LURE_PYTHON', sys.executable),
                    help='python of a venv with lure_tts and mlx installed')
    ap.add_argument('--lure-model', type=Path, help='Lure Turbo model directory')
    ap.add_argument('--lure-voice', type=Path, help='reference voice audio for Lure voice cloning')
    ap.add_argument('--lure-voice-text', help='transcript of --lure-voice')
    ap.add_argument('--lure-seed', type=int, default=1234)
    ap.add_argument('--lure-first-chunk-frames', type=int, default=4, help='codec frames in the first Lure chunk')
    ap.add_argument('--no-lure-snapshot', dest='lure_snapshot', action='store_false',
                    help='disable the decoder-state snapshot (re-prime the codec decoder on every utterance)')
    ap.add_argument('--lure-dump', type=Path,
                    help='write each Lure utterance as a 24 kHz WAV plus utterances.jsonl (id, text) into this dir')
    ap.add_argument('--lure-dump-tag', default='run', help='id prefix for --lure-dump files, e.g. r1_fc4_A')
    ap.add_argument('--interleave-arms', metavar='NAME:FRAMES,...',
                    help='--tts lure only: run every call once per arm back to back inside this one process, e.g. '
                         'fc4_A:4,fc1:1,fc4_B:4 (one warm helper per arm; overrides --lure-first-chunk-frames)')
    ap.add_argument('--interleave-rounds', type=int, help='repeat the --calls list this many times (default 1)')
    ap.add_argument('--interleave-seed', type=int, help='seed of the balanced per-call arm order (default 0)')
    args = ap.parse_args(argv)
    if args.interleave_arms is None:
        if args.interleave_rounds is not None or args.interleave_seed is not None:
            ap.error('--interleave-rounds/--interleave-seed need --interleave-arms')
    else:
        arms = []
        for item in args.interleave_arms.split(','):
            m = re.fullmatch(r'([a-zA-Z0-9_-]+):([0-9]+)', item)
            if not m or not 1 <= int(m[2]) <= 32 or m[1] in dict(arms):
                ap.error('--interleave-arms needs unique NAME:FRAMES items, NAME [a-zA-Z0-9_-], FRAMES 1..32')
            arms.append((m[1], int(m[2])))
        if len(arms) < 2 or args.tts != 'lure' or args.slm:
            ap.error('--interleave-arms needs two or more arms and --tts lure, and does not run with --slm')
        args.interleave_arms = arms
        args.interleave_rounds = 1 if args.interleave_rounds is None else args.interleave_rounds
        args.interleave_seed = 0 if args.interleave_seed is None else args.interleave_seed
        if args.interleave_rounds < 1:
            ap.error('--interleave-rounds must be >= 1')
    if args.tts == 'lure' and not (args.lure_model and args.lure_voice and args.lure_voice_text):
        ap.error('--tts lure needs --lure-model, --lure-voice and --lure-voice-text')
    if args.lure_dump is not None:
        if args.tts != 'lure' or not re.fullmatch(r'[a-zA-Z0-9_-]+', args.lure_dump_tag):
            ap.error('--lure-dump needs --tts lure and a [a-zA-Z0-9_-] --lure-dump-tag')
        args.lure_dump = args.lure_dump.expanduser().resolve()
    if not 1 <= args.lure_first_chunk_frames <= 32:
        ap.error('--lure-first-chunk-frames must be 1..32')
    args.out = args.out.expanduser().resolve()
    if args.out.is_relative_to(HERE.parents[1]):
        ap.error('--out must be outside the worktree')
    if (args.clip <= 0 or not -90 <= args.vad_db < 0 or not 80 <= args.silence_ms <= 2000
            or args.silence_ms % 20 or (args.limit is not None and (not args.slm or args.limit <= 0))):
        ap.error('invalid clip, VAD threshold, silence window or suite limit')
    if not all(re.fullmatch(r'[a-zA-Z0-9_-]+', name) for name in args.calls.split(',')):
        ap.error('invalid call name')
    return args


def main():
    args = parse_args()
    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=1))
    return bool(result['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
