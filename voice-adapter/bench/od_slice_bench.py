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
Speculative start (OD-slice-3, --arm-modes w500,spec150,w500b): in a specN arm STT + model + TTS start on the partial
utterance after N ms of trailing silence (N // 20 quiet frames, the Endpointer's own floor rule: 150 -> 140 ms); a
voiced frame before the commit cancels that speculative turn (its STT / model / TTS wall time is counted as wasted)
and the utterance continues; the unchanged --endpointer-silence-ms endpointer commits the turn. Playback is gated on
the commit (no audible false starts): first audio = max(commit, speculative first audio ready). False cuts are decided
by the commit alone, as in the wN control arm, which runs exactly the OD-slice-2b code path.
--fake-stages STT,MODEL,TTS (ms) replaces the three local stages with fixed sleeps (dry runs; UNSCORED, no GPU).
Early commit (OD-slice-4, --arm-modes spec150,spec150ec,spec150b): a specNec arm is specN plus one more way to
commit: at the earliest moment the CURRENT speculative turn's STT transcript is available AND the endpointer has
counted >= EC_SILENCE_MS (200 ms = 10 quiet frames) of trailing silence AND confident_turn_end(transcript) holds; the
unchanged --endpointer-silence-ms commit still applies if that never happens. An early commit is final: the turn plays.
If speech resumes after it (commit before the true speech end) the row is a false cut ('split before speech end'),
exactly as the control's split. specN rows without 'ec' run the OD-slice-3 path unchanged.
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


EC_SILENCE_MS = 200  # early commit never fires before this much endpointer-counted trailing silence
EC_RULE = 'ec1'  # the preregistered confident-turn-end rule below (OD-slice-4 PREREG.md); never tuned after freeze
EC_CONTINUATION = frozenset(('and', 'but', 'so', 'or', 'because', 'um', 'uh', 'the', 'a', 'to', 'of'))


def confident_turn_end(text):
    """Rule ec1: after stripping whitespace the transcript ends in '?', '.' or '!', has >= 3 words (norm_words), and
    its last word is not a continuation word (EC_CONTINUATION)."""
    t = (text or '').strip()
    if not t or t[-1] not in '?.!':
        return False
    words = norm_words(t)
    return len(words) >= 3 and words[-1] not in EC_CONTINUATION


async def first_of(*aws):
    """Wait until the first of the awaitables finishes; cancel the rest."""
    tasks = [asyncio.ensure_future(x) for x in aws]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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


async def run_arm_turn(stages, pcm, turn_id=0, db=VAD_DB, silence_ms=ENDPOINT_SILENCE_MS, spec_ms=None,
                       clock=time.perf_counter_ns, sleep=asyncio.sleep, ec=None):
    """One arm turn: spec_ms None -> the OD-slice-2b endpointer turn unchanged; else a speculative-start turn
    (ec 'ec' -> with the OD-slice-4 early commit; None -> the OD-slice-3 SPEC path unchanged)."""
    if spec_ms is None:
        return await run_live_turn(stages, pcm, turn_id=turn_id, db=db, silence_ms=silence_ms, clock=clock,
                                   sleep=sleep)
    if ec:
        return await run_spec_turn(stages, pcm, turn_id=turn_id, db=db, silence_ms=silence_ms, spec_ms=spec_ms,
                                   clock=clock, sleep=sleep, early_commit=True)
    return await run_spec_turn(stages, pcm, turn_id=turn_id, db=db, silence_ms=silence_ms, spec_ms=spec_ms,
                               clock=clock, sleep=sleep)


class StageTimer:
    """Wraps the three stages; adds each call's wall time (ns, to completion or cancel) to .ns[stage]."""
    def __init__(self, stages, clock):
        self.ns = {'stt': 0, 'model': 0, 'tts': 0}
        stt, model, tts = stages

        async def t_stt(pcm):
            t = clock()
            try:
                return await stt(pcm)
            finally:
                self.ns['stt'] += clock() - t

        def t_gen(name, fn):
            async def stage(arg):
                t = clock()
                try:
                    async with aclosing(fn(arg)) as stream:
                        async for x in stream:
                            yield x
                finally:
                    self.ns[name] += clock() - t
            return stage

        self.stages = (t_stt, t_gen('model', model), t_gen('tts', tts))


class SpecTurn:
    """One speculative engine turn on a partial utterance. Audio is held until commit(), then played in order."""
    def __init__(self, stages, pcm, turn_id, clock, on_play=None, track_stt=False):
        self.clock, self.on_play = clock, on_play
        self.timer = StageTimer(stages, clock)
        self.held, self.played, self.commit_ns, self.ready_ns = [], [], None, None
        self.transcript, self.stt_done, self.ec_checked = None, asyncio.Event(), False
        run_stages = self.timer.stages
        if track_stt:  # early-commit arms only: expose the STT result the moment it returns (no extra clock reads)
            t_stt, model, tts = run_stages

            async def stt(audio):
                text = await t_stt(audio)
                self.transcript = text
                self.stt_done.set()
                return text
            run_stages = (stt, model, tts)
        self.task = asyncio.ensure_future(run_turn(run_stages, pcm, turn_id=turn_id, clock=clock,
                                                   on_audio=self._audio))

    def _play(self, ns, n):
        self.played.append(ns)
        if self.on_play:
            self.on_play(ns, n)

    def _audio(self, pcm):
        now = self.clock()
        if self.ready_ns is None:
            self.ready_ns = now
        if self.commit_ns is None:
            self.held.append(len(pcm))
        else:
            self._play(now, len(pcm))

    def commit(self, ns):
        self.commit_ns = ns
        for n in self.held:
            self._play(ns, n)
        self.held = []

    async def cancel(self):
        """Drop the turn (held audio is never played); returns the wasted wall time per stage (ns)."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.held = []
        return dict(self.timer.ns)


async def run_spec_turn(stages, pcm, turn_id=0, db=VAD_DB, silence_ms=ENDPOINT_SILENCE_MS, spec_ms=150,
                        clock=time.perf_counter_ns, sleep=asyncio.sleep, on_play=None, early_commit=False):
    """Real-time paced like stream_endpoint, with a speculative engine turn started after spec_ms of trailing
    silence, cancelled on resumed speech, committed when the (unchanged) silence_ms endpointer finalizes.
    early_commit: also commit at the earliest moment the current speculative turn's transcript is known, the
    endpointer has counted >= EC_SILENCE_MS of trailing silence and confident_turn_end(transcript) holds."""
    spec_frames = spec_ms // 20
    if not silence_ms or spec_frames < 1 or spec_frames >= silence_ms // 20:
        raise ValueError('need 20 <= spec_ms < silence_ms (endpointer on)')
    ec_frames = EC_SILENCE_MS // 20
    if early_commit and not spec_frames < ec_frames < silence_ms // 20:
        raise ValueError('early commit needs spec_ms < EC_SILENCE_MS < silence_ms')
    ep, end = _endpointer(db, silence_ms), speech_end_sample(pcm, db)
    t0 = clock()
    speech_end_ns = None if end is None else t0 + (end + 1) * 1_000_000_000 // RATE
    counts = {'spec_started': 0, 'spec_cancelled': 0, 'spec_committed': 0}
    wasted = {'stt': 0, 'model': 0, 'tts': 0}
    spec, spec_t0, commit_ns, utt = None, None, None, None
    ec = {'early_commit': 0, 'ec_rule_evals': 0, 'ec_last_transcript': None, 'ec_last_pass': None,
          'ec_resumed': False, 'ec_commit_quiet_frames': None}

    def try_early(now):
        """Evaluate the rule once per speculative turn, when its transcript and >= ec_frames of silence both exist."""
        nonlocal commit_ns
        if (not early_commit or ec['early_commit'] or spec is None or spec.ec_checked or spec.transcript is None
                or not ep.active or ep.quiet < ec_frames):
            return
        spec.ec_checked = True
        passed = confident_turn_end(spec.transcript)
        ec.update(ec_rule_evals=ec['ec_rule_evals'] + 1, ec_last_transcript=spec.transcript, ec_last_pass=passed)
        if passed:
            commit_ns = now
            ec.update(early_commit=1, ec_commit_quiet_frames=ep.quiet)
            spec.commit(now)

    try:
        for k, frame in enumerate(padded_frames(pcm, silence_ms)):
            due = t0 + (k + 1) * FRAME * 1_000_000_000 // RATE
            while (wait := due - clock()) > 0:
                if (early_commit and not ec['early_commit'] and spec is not None and spec.transcript is None
                        and ep.active and ep.quiet >= ec_frames):  # wake the moment the STT result lands
                    await first_of(sleep(wait / 1e9), spec.stt_done.wait())
                    try_early(clock())
                    continue
                await sleep(wait / 1e9)
                break
            _, out = ep.feed(frame, (k + 1) * FRAME / RATE)
            now = clock()
            if ec['early_commit']:  # committed early: keep listening only to see whether speech resumes
                if out is not None or not ep.active:
                    break
                ec['ec_resumed'] = ec['ec_resumed'] or ep.quiet == 0
                continue
            if out is not None:  # the commit: the same decision the control arm's endpointer makes
                commit_ns, utt = now, out.tobytes()
                break
            if spec is not None and (not ep.active or ep.quiet == 0):  # speech resumed before the commit
                for key, ns in (await spec.cancel()).items():
                    wasted[key] += ns
                counts['spec_cancelled'] += 1
                spec = None
            if spec is None and ep.active and ep.quiet == spec_frames and ep.voiced >= 6:
                spec, spec_t0 = SpecTurn(stages, np.concatenate(ep.active).tobytes(), turn_id, clock, on_play,
                                         **({'track_stt': True} if early_commit else {})), now
                counts['spec_started'] += 1
            try_early(now)
    except BaseException:
        if spec is not None:
            await spec.cancel()
        raise
    extra: dict = dict(counts, **{f'wasted_{key}_ms': ns / 1e6 for key, ns in wasted.items()})
    if early_commit:
        extra.update(ec, ec_rule=EC_RULE)
    if commit_ns is None or speech_end_ns is None:
        if spec is not None:
            await spec.cancel()
        row = {'turn_id': turn_id, 'ok': False, 'error_stage': 'endpointer', 'error': 'no utterance finalized'}
        row.update({k: None for k in METRICS}, **extra)
        return row
    if spec is not None:
        extra['spec_committed'] = 1
    if commit_ns < speech_end_ns:  # split: as in the control arm the committed turn would play; no latency row
        if spec is not None:
            if ec['early_commit']:
                await asyncio.gather(spec.task, return_exceptions=True)  # an early commit is final: it plays
            else:
                await spec.cancel()  # bench economy only; the split is the endpointer's decision, not the spec's
        row = {'turn_id': turn_id, 'ok': False, 'error_stage': 'endpointer', 'error': 'split before speech end',
               'endpointer_declared_ms': (commit_ns - speech_end_ns) / 1e6, 'transcript': None, 'reply': None}
        if ec['early_commit'] and spec is not None:
            row['transcript'] = spec.transcript
        row.update({k: None for k in METRICS}, **extra)
        return row
    if spec is None:  # e.g. the 15 s cap finalized mid-speech: no speculative turn to commit, run it plainly
        row = await run_turn(stages, utt, turn_id=turn_id, clock=clock)
        row.update(extra, spec_fallback=True, endpointer_declared_ms=(commit_ns - speech_end_ns) / 1e6)
        if row['ok']:
            row['endpointer_ms'] = (row['eos_ns'] - speech_end_ns) / 1e6
            row['speech_end_to_first_audio_ms'] = row['endpointer_ms'] + row['eos_to_first_audio_ms']
        return row
    if not ec['early_commit']:
        spec.commit(commit_ns)
    row = await spec.task
    ms = (lambda ns: ns / 1e6)
    row.update(extra, commit_ns=commit_ns, endpointer_declared_ms=ms(commit_ns - speech_end_ns),
               spec_start_ms=ms(spec_t0 - speech_end_ns), spec_fallback=False,
               spec_eos_to_first_audio_ms=row['eos_to_first_audio_ms'])  # spec start -> first audio ready
    if row['ok'] and spec.played:
        first = spec.played[0]
        row.update(endpointer_ms=ms(commit_ns - speech_end_ns),
                   first_audio_ready_ms=ms(spec.ready_ns - speech_end_ns),
                   gate_hold_ms=ms(max(0, commit_ns - spec.ready_ns)),
                   eos_to_first_audio_ms=ms(first - commit_ns),  # commit -> first played audio
                   speech_end_to_first_audio_ms=ms(first - speech_end_ns))
    else:
        row.update({k: None for k in METRICS}, ok=False, first_audio_ready_ms=None, gate_hold_ms=None)
    return row


async def run_turn(stages, pcm, turn_id=0, clock=time.perf_counter_ns, on_audio=None):
    """One ChainedEngine turn; returns a row with the four stage gaps and EOS -> first audio (ms)."""
    marks = {}

    def trace(event, **_):
        if event == 'chained.eos':
            marks['t0'] = clock()

    def emit(event):
        if event.get('type') == 'output_transcript.added' and 'clause' not in marks:
            marks['clause'], marks['clause_text'] = clock(), event['text']

    engine = ce.ChainedEngine(*stages, trace=trace, emit=emit, on_audio=on_audio, clock=clock)
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


def mode_arms(spec, commit_ms):
    """'w500,spec150,w500b' -> [('W500', 500, None), ('SPEC150', 500, 150), ('W500b', 500, None)].
    wN = endpointer window N (control, the OD-slice-2b path); specN = speculative start after N ms of silence,
    committed by the --endpointer-silence-ms window. A trailing letter is an explicit replicate label (A/A); unlabeled
    repeats get b, c, ... as in window_arms.
    specNec (OD-slice-4) = specN plus the early commit: ('SPEC150EC', 500, 150, 'ec'); only these are 4-tuples.
    """
    arms, seen = [], {}
    for tok in (t.strip().lower() for t in spec.split(',') if t.strip()):
        m = re.fullmatch(r'(w|spec)(\d+)(ec)?([a-z]?)', tok)
        if not m or (m.group(3) and (m.group(1) != 'spec' or m.group(4))):
            raise ValueError(f'bad arm mode {tok!r}')
        kind, ms, ec, tag = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if kind == 'w' and (ms % 20 or not 80 <= ms <= 2000):
            raise ValueError(f'{tok}: window must be a multiple of 20 in 80..2000')
        if kind == 'spec' and (ms % 10 or not 20 <= ms < commit_ms):
            raise ValueError(f'{tok}: speculative start must be a multiple of 10 in 20..(commit window - 10)')
        if ec:
            if not ms // 20 < EC_SILENCE_MS // 20 < commit_ms // 20:
                raise ValueError(f'{tok}: early commit needs spec start < {EC_SILENCE_MS} ms < commit window')
            if f'SPEC{ms}EC' in seen:
                raise ValueError(f'{tok}: repeated early-commit arm')
            seen[f'SPEC{ms}EC'] = 1
            arms.append((f'SPEC{ms}EC', commit_ms, ms, 'ec'))
            continue
        base = f'W{ms}' if kind == 'w' else f'SPEC{ms}'
        k = seen.get(base, 0)
        seen[base] = k + 1
        label = base + (tag or ('' if not k else chr(ord('a') + k)))
        arms.append((label, ms, None) if kind == 'w' else (label, commit_ms, ms))
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
    out: dict = {'n': len(ok), 'errors': len(rows) - len(ok),
                 'premature_endpoints': sum(r.get('error') == 'split before speech end' for r in rows)}
    out['false_cuts'] = out['premature_endpoints']
    if any('spec_started' in r for r in rows):  # speculative arms: counts and wasted wall time over all rows
        for k in ('spec_started', 'spec_cancelled', 'spec_committed', 'wasted_stt_ms', 'wasted_model_ms',
                  'wasted_tts_ms'):
            out[k] = sum(r.get(k) or 0 for r in rows)
        for k in ('spec_start_ms', 'first_audio_ready_ms', 'gate_hold_ms'):
            xs = [r[k] for r in ok if r.get(k) is not None]
            out[k] = {'p50': pct(xs, .5), 'p95': pct(xs, .95)}
    if any('early_commit' in r for r in rows):  # early-commit arms: fire rate and the false cuts it caused
        fired = [r for r in rows if r.get('early_commit')]
        out['early_commits'] = len(fired)
        out['ec_fire_rate'] = len(fired) / len(rows) if rows else None
        out['ec_false_cuts'] = sum(r.get('error') == 'split before speech end' for r in fired)
        out['ec_rule_evals'] = sum(r.get('ec_rule_evals') or 0 for r in rows)
        xs = [r['endpointer_ms'] for r in ok if r.get('early_commit') and r.get('endpointer_ms') is not None]
        out['ec_commit_ms'] = {'p50': pct(xs, .5), 'p95': pct(xs, .95)}
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
    pair = next((p for p in (('A', 'A2'), ('W500', 'W500b'), ('SPEC150', 'SPEC150b')) if set(p) <= arms.keys()),
                None)
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
        self.stale = False  # a cancelled request's reply is still due on stdout

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
        if self.stale:  # a speculative turn was cancelled mid-request: discard its late reply first
            await self._line()
        self.stale = True
        self.proc.stdin.write(struct.pack('<I', len(pcm) // 2) + pcm)
        await self.proc.stdin.drain()
        text = json.loads(await self._line())['text'].strip()
        self.stale = False
        return text


def sleep_stages(stt_ms, model_ms, tts_ms):
    """Dry-run stages (UNSCORED): fixed asyncio sleeps, no model, no GPU; EOS -> first audio ~= the sum."""
    async def stt(pcm):
        await asyncio.sleep(stt_ms / 1000)
        return 'Is this a fake transcript?'  # passes rule ec1, so dry runs exercise the early commit

    async def model(text):
        await asyncio.sleep(model_ms / 1000)
        yield 'This is a fake reply. '
        await asyncio.sleep(0.05)
        yield 'Done.'

    async def tts(clause):
        await asyncio.sleep(tts_ms / 1000)
        for _ in range(3):
            yield b'\x00\x00' * 480
            await asyncio.sleep(0.02)

    return stt, model, tts


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


async def real_stages(a, stack):
    """The three local stages (MLX Whisper helper, private llama-server, Lure helper), closed by stack."""
    import local_voice_replay as lvr
    from aiohttp import ClientSession, ClientTimeout
    http = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=60), trust_env=False))
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

    return stt.stt, model, as_bytes(lure.tts)


async def bench(a):
    manifest = json.loads((a.utterances / 'manifest.json').read_text())
    utts = [(m, read_wav(a.utterances / m['file'])) for m in manifest]
    arms = (mode_arms(a.arm_modes, a.endpointer_silence_ms) if a.arm_modes else
            [(label, ms, None) for label, ms in window_arms(a.arm_windows)] if a.arm_windows else None)
    plan = schedule_windows(a.runs, len(utts), a.seed, arms) if arms else schedule(a.runs, len(utts), a.seed)
    rows, aborted, t_start = [], None, time.monotonic()
    async with AsyncExitStack() as stack:
        stages = sleep_stages(*a.fake_stages) if a.fake_stages else await real_stages(a, stack)
        for i in range(a.warmup):
            arm = arms[i % len(arms)] if arms else (None, a.endpointer_silence_ms, None)
            row = await run_arm_turn(stages, utts[i % len(utts)][1], turn_id=-1 - i, db=a.vad_db,
                                     silence_ms=arm[1], spec_ms=arm[2], ec=arm[3] if len(arm) > 3 else None)
            print(json.dumps({'warmup': i, 'ok': row['ok'], 'e2a': row['eos_to_first_audio_ms'],
                              'se2a': row['speech_end_to_first_audio_ms']}), file=sys.stderr)
        turn = 0
        for p in plan:
            if not gate_ok(a.gate):
                aborted = f'gate before run {p["run"]} {p["arm"]}'
                break
            turns = p.get('turns') or [(idx, p['arm'], a.endpointer_silence_ms, None) for idx in p['order']]
            for idx, arm, window, spec_ms, *ec in turns:
                if arms and not gate_ok(a.gate):  # sweep: gate before every arm turn
                    aborted = f'gate before run {p["run"]} {arm} turn {turn}'
                    break
                m, pcm = utts[idx]
                row = await run_arm_turn(stages, pcm, turn_id=turn, db=a.vad_db, silence_ms=window, spec_ms=spec_ms,
                                         ec=ec[0] if ec else None)
                turn += 1
                if spec_ms is not None:
                    row['spec_ms'] = spec_ms
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
            'vad_db': a.vad_db, 'endpointer_silence_ms': a.endpointer_silence_ms,
            'arm_windows': window_arms(a.arm_windows) if a.arm_windows else None,
            **({'arm_modes': arms} if a.arm_modes else {}),
            **({'fake_stages_ms': list(a.fake_stages), 'scored': False} if a.fake_stages else {}),
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
    ap.add_argument('--lure-model', type=Path)
    ap.add_argument('--lure-voice', type=Path)
    ap.add_argument('--lure-voice-text')
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
    ap.add_argument('--arm-modes', default='',
                    help='speculative-start arms, e.g. w500,spec150,w500b: wN = endpointer window N (control), specN '
                         '= speculative start after N ms of silence committed by --endpointer-silence-ms; specNec = '
                         'specN plus the early commit on a confident turn end (rule ec1, >= 200 ms silence); '
                         'interleaved per utterance like --arm-windows (exclusive with it); default off')
    ap.add_argument('--fake-stages', default='',
                    help='STT,MODEL,TTS fixed sleeps in ms (dry run, UNSCORED): no Whisper / llama.cpp / Lure')
    ap.add_argument('--gate', help='command run before every pass; nonzero exit aborts the bench')
    a = ap.parse_args(argv)
    try:
        a.fake_stages = tuple(int(x) for x in a.fake_stages.split(',')) if a.fake_stages else ()
    except ValueError:
        a.fake_stages = (-1,)
    if a.fake_stages and (len(a.fake_stages) != 3 or min(a.fake_stages) < 0):
        ap.error('--fake-stages needs three non-negative ints: STT,MODEL,TTS ms')
    if a.make_utterances is None and not (a.out and a.utterances and (
            a.fake_stages or (a.whisper_model and a.gguf))):
        ap.error('bench needs --out, --utterances, --whisper-model and --gguf (or use --make-utterances)')
    if (a.make_utterances is not None or not a.fake_stages) and not (
            a.lure_model and a.lure_voice and a.lure_voice_text):
        ap.error('--lure-model, --lure-voice and --lure-voice-text are required (except with --fake-stages)')
    if a.arm_modes and a.arm_windows:
        ap.error('--arm-modes and --arm-windows are exclusive')
    if a.arm_modes:
        try:
            if not a.endpointer_silence_ms:
                raise ValueError('needs the endpointer on')
            mode_arms(a.arm_modes, a.endpointer_silence_ms)
        except ValueError as exc:
            ap.error(f'--arm-modes: {exc}')
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
