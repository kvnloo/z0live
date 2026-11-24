#!/usr/bin/env python3
"""Lure MLX TTS helper for local_voice_replay.py --tts lure. Run it with the python of a venv that has lure_tts + mlx.

Loads the Turbo model once, clones one reference voice, optionally enables the decoder-state snapshot
(lure_decoder_snapshot.py), warms up, then writes a ready frame. Protocol (see LureTTS in local_voice_replay.py):
stdin JSON lines {"text": ...} or {"cancel": true}; stdout frames of <uint32 sample count> + float32 24 kHz PCM, a zero
count ends each utterance. One JSON line of timing per utterance goes to stderr.
"""
import argparse
import json
import os
from pathlib import Path
import select
import struct
import sys
import tempfile
import time

os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
HERE = Path(__file__).resolve().parent
WARM = ('What is the capital of France?', 'Sure, give me one second to check that.')


class Requests:
    """Line reader on the raw stdin fd, so select() sees exactly what is still unread."""
    def __init__(self):
        self.fd, self.buf = sys.stdin.fileno(), b''

    def _line(self):
        i = self.buf.find(b'\n')
        if i < 0:
            return None
        line, self.buf = self.buf[:i], self.buf[i + 1:]
        return json.loads(line)

    def get(self, block=True):
        while (req := self._line()) is None:
            if not block and not select.select([self.fd], [], [], 0)[0]:
                return None
            data = os.read(self.fd, 65536)
            if not data:
                return EOFError
            self.buf += data
        return req


def model_view(model):
    """Lure.load wants the turbo head passed separately; link everything else into a private temp dir."""
    view = Path(tempfile.mkdtemp(prefix='lure-view-'))
    for p in model.iterdir():
        if p.name != 'turbo_head.safetensors' and not p.name.startswith('.'):
            (view / p.name).symlink_to(p)
    head = model / 'turbo_head.safetensors'
    return view, (str(head) if head.exists() else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model', type=Path, required=True)
    ap.add_argument('--voice', type=Path, required=True)
    ap.add_argument('--voice-text', required=True)
    ap.add_argument('--seed', type=int, default=1234)
    ap.add_argument('--first-chunk-frames', type=int, default=4)
    ap.add_argument('--no-snapshot', dest='snapshot', action='store_false')
    a = ap.parse_args()
    out = sys.stdout.buffer

    def frame(pcm=()):
        out.write(struct.pack('<I', len(pcm)))
        if len(pcm):
            out.write(pcm.astype('<f4', copy=False).tobytes())
        out.flush()

    def log(**kv):
        print(json.dumps(kv), file=sys.stderr, flush=True)

    import numpy as np
    from lure_tts import Lure
    t0 = time.perf_counter()
    view, head = model_view(a.model.expanduser().resolve(strict=True))
    tts = Lure.load(str(view), backend='mlx', **({'turbo_head': head} if head else {}))
    voice = tts.make_voice(str(a.voice.expanduser().resolve(strict=True)), a.voice_text, name='bench-replay')
    cache = None
    if a.snapshot:
        sys.path.insert(0, str(HERE))
        import lure_decoder_snapshot as ds
        cache = ds.enable_decoder_snapshot(tts.backend)

    def speak(text, reqs=None):
        s0 = cache.stats() if cache is not None else None  # an empty cache has len 0, so never test it for truth
        start, first, n, samples, cancelled = time.perf_counter(), None, 0, 0, False
        stream = tts.stream(text, voice, seed=a.seed, first_chunk_frames=a.first_chunk_frames)
        try:
            for pcm in stream:
                x = np.asarray(pcm, dtype=np.float32).reshape(-1)
                if len(x):
                    first = first or time.perf_counter()
                    n, samples = n + 1, samples + len(x)
                    if reqs is not None:
                        frame(x)
                if reqs is not None and (r := reqs.get(block=False)) is not None:
                    if r is EOFError or 'cancel' in r:
                        cancelled = True
                        break
        finally:
            stream.close()
        snap = None
        if cache is not None:
            s1 = cache.stats()
            snap = {k: s1[k] - s0[k] for k in ('hits', 'misses', 'captures')}
        log(event='utterance', chars=len(text), ttfa_ms=round((first - start) * 1000, 2) if first else None,
            wall_ms=round((time.perf_counter() - start) * 1000, 2), chunks=n, audio_s=round(samples / 24000, 3),
            first_chunk_frames=a.first_chunk_frames, snapshot=a.snapshot, snap=snap, cancelled=cancelled)

    for text in WARM:  # the first call primes (and, with the snapshot on, captures) the decoder state
        speak(text)
    log(event='ready', load_s=round(time.perf_counter() - t0, 2), first_chunk_frames=a.first_chunk_frames,
        snapshot=a.snapshot, seed=a.seed)
    frame()
    reqs = Requests()
    try:
        while (req := reqs.get()) is not EOFError:
            if 'text' in req:
                speak(req['text'], reqs)
                frame()
    finally:
        tts.close()


if __name__ == '__main__':
    main()
