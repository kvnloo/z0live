#!/usr/bin/env python3
"""MLX Whisper STT helper for od_slice_bench.py. Run it with the python of a venv that has mlx-audio (e.g. the Lure venv).

Loads one local Whisper model directory once (mlx_audio.stt.load; needs the HF tokenizer/processor files next to the
MLX weights), warms up, then prints {"event": "ready"}. Protocol: stdin frames of <uint32 sample count> + int16 16 kHz
mono PCM; one stdout JSON line {"text": ..., "ms": ...} per frame. Offline only.
"""
import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time

os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model', type=Path, required=True, help='local Whisper MLX model dir with tokenizer files')
    ap.add_argument('--language', default='en')
    a = ap.parse_args()

    import numpy as np
    from mlx_audio.stt import load

    t0 = time.perf_counter()
    model = load(str(a.model.expanduser().resolve(strict=True)))

    def transcribe(pcm):
        out = model.generate(pcm, language=a.language, temperature=0.0, condition_on_previous_text=False,
                             return_timestamps=False, verbose=None)
        return (getattr(out, 'text', None) or '').strip()

    t = np.linspace(0, 1, 16000, dtype=np.float32)
    transcribe((0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))  # compile/warm the graph once
    print(json.dumps({'event': 'load', 'load_s': round(time.perf_counter() - t0, 2)}), file=sys.stderr, flush=True)
    print(json.dumps({'event': 'ready'}), flush=True)
    inp = sys.stdin.buffer
    while len(head := inp.read(4)) == 4:
        n, = struct.unpack('<I', head)
        raw = inp.read(2 * n)
        pcm = np.frombuffer(raw, '<i2').astype(np.float32) / 32768.0
        s = time.perf_counter()
        text = transcribe(pcm)
        print(json.dumps({'text': text, 'ms': round((time.perf_counter() - s) * 1000, 2)}), flush=True)


if __name__ == '__main__':
    main()
