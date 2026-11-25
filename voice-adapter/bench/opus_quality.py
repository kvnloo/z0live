"""Opus downlink perceptual-quality harness (F4q-quality): PESQ-WB (ITU-T P.862.2, pip `pesq`) and STOI (pip `pystoi`)
of the mono Opus downlink encoder (opus_mono.MonoOpusEncoder) per arm and fixture.

Arms (all MonoOpusEncoder, 96 kbit/s): A = complexity unset (libopus default 10), A2 = A again (A/A control; the
encoder is deterministic, so A2 must equal A), B = complexity 5, C = complexity 3 (descriptive only).

Per fixture and arm:
  1. the fixture (mono PCM16, resampled to 48 kHz with PyAV when needed) is the reference = the exact encoder input;
  2. 20 ms frames go through the encoder, then two frames of digital silence (flushes the lookahead);
  3. every packet is decoded with aiortc's OpusDecoder (the same decoder for every arm; stereo out, left channel used,
     L == R checked);
  4. the decoded signal is aligned to the reference by the cross-correlation peak over lags 0..4800 samples
     (the expected value is the libopus encoder lookahead/pre-skip, 312 samples at 48 kHz; the found lag is recorded);
  5. reference and aligned decode are cut to the reference length and decimated 48 kHz -> 16 kHz with
     scipy.signal.resample_poly(x, 1, 3) (identical filter for both);
     PESQ-WB = pesq(16000, ref16, deg16, 'wb'); STOI = pystoi.stoi(ref48, deg48, 48000) (codec rate; pystoi
     resamples to 10 kHz internally); waveform SNR (dB) is kept for comparison with the F4q gate.

Fixtures: up48.wav / reply24.wav exactly as transport_bench.fixture() renders them (macOS `say`, or the CI espeak-ng
`say` shim in bench/ghci/f4o), plus four extra 48 kHz sentences (EXTRA below) rendered by the same `say` call.

Usage (from voice-adapter/): python -I bench/opus_quality.py <fixtures_dir> <out.json>
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import av  # noqa: E402
import numpy as np  # noqa: E402
from aiortc.codecs import opus as aopus  # noqa: E402
from aiortc.jitterbuffer import JitterFrame  # noqa: E402
from pesq import pesq  # noqa: E402
from pystoi import stoi  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

import opus_mono  # noqa: E402
import transport_bench as tb  # noqa: E402

VERSION = "opus_quality/1"
FRAME = 960
MAX_LAG = 4800
ARMS = {"A": None, "A2": None, "B": 5, "C": 3}  # complexity; None = option not set (libopus default 10)
BIT_RATE = 96000
EXTRA = {
    "s1_48.wav": "The quick brown fox jumps over the lazy dog, and then it runs back across the field.",
    "s2_48.wav": "Please set a timer for twelve minutes, and tell me when the pasta is ready to drain.",
    "s3_48.wav": "Seven thousand, three hundred and forty two. That number again: seven, three, four, two.",
    "s4_48.wav": "She sells sea shells by the sea shore; the shells she sells are surely sea shells.",
}


def read_pcm(path: Path, rate: int, text: str) -> np.ndarray:
    return np.frombuffer(tb.fixture(path, text, rate), dtype=np.int16)


def to48(rate: int, pcm: np.ndarray) -> np.ndarray:
    if rate == 48000:
        return pcm
    rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
    f = av.AudioFrame(format="s16", layout="mono", samples=len(pcm))
    f.planes[0].update(pcm.tobytes())
    f.sample_rate, f.pts, f.time_base = rate, 0, Fraction(1, rate)
    outs = list(rs.resample(f)) + list(rs.resample(None))
    return np.concatenate([np.frombuffer(o.planes[0], dtype=np.int16, count=o.samples) for o in outs])


def encode_decode(complexity, src: np.ndarray) -> dict:
    enc = opus_mono.MonoOpusEncoder(bit_rate=BIT_RATE, complexity=complexity)
    dec = aopus.OpusDecoder()
    pad = np.concatenate([src, np.zeros((-len(src)) % FRAME + 2 * FRAME, dtype=np.int16)])
    sizes, chunks, lr_equal, h = [], [], True, hashlib.sha256()
    for i in range(len(pad) // FRAME):
        f = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        f.planes[0].update(pad[i * FRAME:(i + 1) * FRAME].tobytes())
        f.sample_rate, f.pts, f.time_base = 48000, i * FRAME, Fraction(1, 48000)
        payloads, ts = enc.encode(f)
        for p in payloads:
            sizes.append(len(p))
            h.update(len(p).to_bytes(4, "little") + p)
            for fr in dec.decode(JitterFrame(p, ts)):
                st = np.frombuffer(fr.planes[0], dtype=np.int16, count=fr.samples * 2).reshape(-1, 2)
                lr_equal &= bool(np.array_equal(st[:, 0], st[:, 1]))
                chunks.append(st[:, 0].astype(np.float64))
    return {"decoded": np.concatenate(chunks), "sizes": sizes, "lr_equal": lr_equal, "sha256": h.hexdigest(),
            "complexity_attr": enc.complexity, "bit_rate": enc.codec.bit_rate}


def align(dec: np.ndarray, ref: np.ndarray) -> int:
    """Lag L maximising sum dec[k + L] * ref[k] over L in [0, MAX_LAG] (FFT cross-correlation)."""
    n = len(dec) + len(ref)
    nfft = 1 << (n - 1).bit_length()
    xc = np.fft.irfft(np.fft.rfft(dec, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)
    return int(np.argmax(xc[:MAX_LAG + 1]))


def score(arm: str, complexity, ref48: np.ndarray) -> dict:
    r = encode_decode(complexity, ref48)
    d = r["decoded"]
    lag = align(d, ref48.astype(np.float64))
    deg48 = d[lag:lag + len(ref48)]
    ref = ref48.astype(np.float64)[:len(deg48)]
    err = ref - deg48
    snr = 10 * np.log10(float(np.dot(ref, ref)) / max(float(np.dot(err, err)), 1e-9))
    ref16, deg16 = resample_poly(ref, 1, 3), resample_poly(deg48, 1, 3)
    return {"arm": arm, "complexity": complexity, "complexity_attr": r["complexity_attr"], "bit_rate": r["bit_rate"],
            "packets": len(r["sizes"]), "bytes_mean": round(float(np.mean(r["sizes"])), 2),
            "payload_sha256": r["sha256"], "l_equals_r": r["lr_equal"], "lag_samples": lag,
            "ref_samples_48k": len(ref48), "scored_samples_48k": len(deg48),
            "pesq_wb": round(float(pesq(16000, ref16, deg16, "wb")), 4),
            "stoi": round(float(stoi(ref, deg48, 48000, extended=False)), 5),
            "snr_db": round(float(snr), 3)}


def main() -> int:
    fx, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    fx.mkdir(parents=True, exist_ok=True)
    fixtures = {"up48.wav": (48000, tb.UP_TEXT), "reply24.wav": (24000, tb.REPLY_TEXT)}
    fixtures.update({k: (48000, v) for k, v in EXTRA.items()})
    res = {"version": VERSION, "bit_rate": BIT_RATE, "arms": {k: v for k, v in ARMS.items()},
           "align": f"cross-correlation peak, lags 0..{MAX_LAG} at 48 kHz", "resample_16k": "resample_poly(1,3)",
           "env": {"python": sys.version.split()[0], "machine": platform.machine(), "av": av.__version__,
                   "ffmpeg_libs": {k: ".".join(map(str, v)) for k, v in av.library_versions.items()}},
           "fixtures": {}, "results": {}}
    for name, (rate, text) in fixtures.items():
        pcm = read_pcm(fx / name, rate, text)
        res["fixtures"][name] = {"rate": rate, "samples": len(pcm),
                                 "sha256": hashlib.sha256((fx / name).read_bytes()).hexdigest()}
        ref48 = to48(rate, pcm)
        res["results"][name] = {}
        for arm, cx in ARMS.items():
            r = score(arm, cx, ref48)
            res["results"][name][arm] = r
            print(name, arm, json.dumps(r), flush=True)
    out_path.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
