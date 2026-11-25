"""F4o quality gate: encode the bench's own fixtures with stock (A, A2) and mono (B) encoders, decode with aiortc's
OpusDecoder (stereo out, as the phone/aiortc peer would), compare with the source PCM.

Usage (from the F4o worktree voice-adapter/):
  env -u PYTHONPATH <venv python> -I <this file> <adapter_dir> <fixtures_dir> <out.json>
Per arm and fixture: packets, decoded frames, decoded frame lengths, L==R check, best lag (samples) found by
cross-correlation in [0, 4800], SNR dB and mean abs error (int16 units) over the aligned overlap, payload bytes.
"""
import json
import sys
import wave
from fractions import Fraction

adapter_dir, fx, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, adapter_dir)

import av  # noqa: E402
import numpy as np  # noqa: E402
from aiortc.codecs import opus as aopus  # noqa: E402
from aiortc.jitterbuffer import JitterFrame  # noqa: E402

import opus_mono  # noqa: E402

FRAME = 960


def read(path):
    with wave.open(path, "rb") as w:
        return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def to48(rate, pcm):
    if rate == 48000:
        return pcm
    rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
    f = av.AudioFrame(format="s16", layout="mono", samples=len(pcm))
    f.planes[0].update(pcm.tobytes())
    f.sample_rate, f.pts, f.time_base = rate, 0, Fraction(1, rate)
    outs = list(rs.resample(f)) + list(rs.resample(None))
    return np.concatenate([np.frombuffer(o.planes[0], dtype=np.int16, count=o.samples) for o in outs])


def run(enc, src):
    n = len(src) // FRAME
    dec = aopus.OpusDecoder()
    sizes, dl, lr_equal, chunks, toc_st = [], [], True, [], []
    for i in range(n):
        f = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        f.planes[0].update(src[i * FRAME:(i + 1) * FRAME].tobytes())
        f.sample_rate, f.pts, f.time_base = 48000, i * FRAME, Fraction(1, 48000)
        payloads, ts = enc.encode(f)
        for p in payloads:
            sizes.append(len(p))
            toc_st.append(bool(p[0] & 0x04))
            for fr in dec.decode(JitterFrame(p, ts)):
                st = np.frombuffer(fr.planes[0], dtype=np.int16, count=fr.samples * 2).reshape(-1, 2)
                dl.append(fr.samples)
                lr_equal &= bool(np.array_equal(st[:, 0], st[:, 1]))
                chunks.append(st[:, 0].astype(np.float64))
    d = np.concatenate(chunks)
    s = src[:n * FRAME].astype(np.float64)
    m = min(len(d), len(s))
    # lag: decoded[k + lag] ~ source[k]
    best, best_c = 0, -np.inf
    for lag in range(0, 4801):
        c = float(np.dot(d[lag:m], s[:m - lag]))
        if c > best_c:
            best, best_c = lag, c
    dd, ss = d[best:m], s[:m - best]
    err = ss - dd
    snr = 10 * np.log10(float(np.dot(ss, ss)) / max(float(np.dot(err, err)), 1e-9))
    return {"encoder": type(enc).__name__, "input_frames": n, "packets": len(sizes), "decoded_frames": len(dl),
            "decoded_frame_lengths": sorted(set(dl)), "decoded_samples": int(sum(dl)), "l_equals_r": lr_equal,
            "lag_samples": best, "snr_db": round(snr, 3), "mae_int16": round(float(np.mean(np.abs(err))), 3),
            "bytes_mean": round(float(np.mean(sizes)), 2), "bytes_p50": float(np.median(sizes)),
            "toc_stereo_packets": int(sum(toc_st))}


res = {"arms": {}}
srcs = {}
for name in ("up48.wav", "reply24.wav"):
    rate, pcm = read(f"{fx}/{name}")
    srcs[name] = to48(rate, pcm)
for arm in ("A", "B", "A2"):
    res["arms"][arm] = {}
    for name, src in srcs.items():
        enc = opus_mono.MonoOpusEncoder() if arm == "B" else aopus.OpusEncoder()
        r = run(enc, src)
        res["arms"][arm][name] = r
        print(arm, name, json.dumps(r))
with open(out_path, "w") as fh:
    json.dump(res, fh, indent=1)
