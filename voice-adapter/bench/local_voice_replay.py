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
import re
import signal
import socket
import subprocess
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


class Models:
    def __init__(self, http, asr_url, tts_url, gemma):
        self.http, self.asr_url, self.tts_url, self.gemma = http, asr_url, tts_url, gemma

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
                        yield np.repeat((np.clip(pcm, -1, 1) * 32767).astype('<i2'), 2)


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


async def conversation(models, pcm, db, silence_ms, context='', turns=(), drain=False):
    history = list(turns)
    rows, task = [], None
    start = time.monotonic()
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
                rows.append(row)
                task = asyncio.create_task(answer(utterance, row))
        if drain and task is not None:
            await task
    finally:
        await interrupt('clip_end')
    for row in rows:
        row['marks'] = {k: round(v - start, 4) for k, v in row['marks'].items()}
    size = max(len(pcm), len(playback.pcm)) if drain else len(pcm)
    return bytes(playback.pcm[:size]).ljust(size, b'\0'), rows


async def replay(models, args):
    per, offsets, bc, minutes, errors = {}, [], 0, 0.0, []
    for name in args.calls.split(','):
        try:
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
                ai=ai, per_call=per, out=str(args.out))


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
        subprocess.run(['say', '-v', 'Samantha' if i % 2 else 'Alex', '-o', str(source), case['ask']], check=True)
        voice = dt.pcm(source)
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
            asr, tts = [await stack.enter_async_context(server(liquid, liquid_args, args.out / f'{mode}.log',
                                                              'Server ready at {url}')) for mode in ('asr', 'tts')]
            gemma = await stack.enter_async_context(server(args.llama_server,
                ['-m', args.gemma.expanduser().resolve(strict=True), '-c', '4096', '-ngl', '99', '-np', '1', '--jinja',
                 '--no-webui', '--reasoning', 'off'], args.out / 'gemma.log', 'listening on {url}'))
            pipe = Models(http, asr, tts, gemma)
            warm = [samples async for samples in pipe.tts('What is the capital of France?')]
            if not warm:
                raise RuntimeError('Warm-up produced no audio')
            text = await pipe.asr(np.concatenate(warm)[::3], {})
            if not text or not await pipe.reply([message('system', SYSTEM), message('user', text)], {}):
                raise RuntimeError('Warm-up produced no text')
            return await (spoken_suite(pipe, args) if args.slm else replay(pipe, args))
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main():
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
    args = ap.parse_args()
    args.out = args.out.expanduser().resolve()
    if args.out.is_relative_to(HERE.parents[1]):
        ap.error('--out must be outside the worktree')
    if (args.clip <= 0 or not -90 <= args.vad_db < 0 or not 80 <= args.silence_ms <= 2000
            or args.silence_ms % 20 or (args.limit is not None and (not args.slm or args.limit <= 0))):
        ap.error('invalid clip, VAD threshold, silence window or suite limit')
    if not all(re.fullmatch(r'[a-zA-Z0-9_-]+', name) for name in args.calls.split(',')):
        ap.error('invalid call name')
    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=1))
    return bool(result['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
