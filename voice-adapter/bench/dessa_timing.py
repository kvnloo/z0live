"""Turn-taking timing of a two-track conversation, measured the way ai.thesatyajit.com/articles/dessa measured
Block's Dessa demos (so our numbers compare with theirs: Dessa median floor-take +0.24 s, 29.5% overlap starts).

Per track: 16 kHz mono, RMS in 10 ms frames; voiced = 18 dB above the track's 10th-percentile floor and above
-50 dBFS; talkspurts bridge pauses < 180 ms and drop bursts < 90 ms. A floor-take ("switch") is a talkspurt that
starts after the OTHER speaker spoke last; its offset = start - end of the other's last talkspurt (negative =
started in overlap). A backchannel = a talkspurt entirely inside the other's talkspurt.

  python dessa_timing.py <dir with NAME-speaker1/2 audio>   -> JSON summary on stdout
The AI track is the one with the quieter floor: model output written to disk sits near digital silence, a
microphone track does not (true of every Dessa demo, per the article).
Needs ffmpeg on PATH (decodes FLAC/WAV/MP3).
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

FRAME = 160  # 10 ms at 16 kHz


def pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def spurts(x):
    n = len(x) // FRAME
    rms = np.sqrt(np.mean(x[: n * FRAME].reshape(n, FRAME) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    voiced = (db > np.percentile(db, 10) + 18) & (db > -50)
    out, start = [], None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        if not v and start is not None:
            out.append([start, i]); start = None
    if start is not None:
        out.append([start, n])
    merged = []
    for s, e in out:  # bridge pauses under 180 ms
        if merged and s - merged[-1][1] < 18:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s / 100, e / 100) for s, e in merged if e - s >= 9]  # drop bursts under 90 ms


def timing(a, b):
    """Floor-takes and backchannels of speaker a against speaker b."""
    events = sorted([(s, e, "a") for s, e in a] + [(s, e, "b") for s, e in b])
    offsets, backchannels, prev = [], 0, None
    for s, e, who in events:
        if who == "a" and prev is not None and prev[2] == "b":  # b spoke last: a takes (or tries) the floor
            if prev[0] <= s and e <= prev[1]:
                backchannels += 1  # entirely inside b's talkspurt
            else:
                offsets.append(s - prev[1])
        if who == "b" or not (prev is not None and prev[2] == "b" and prev[0] <= s and e <= prev[1]):
            prev = (s, e, who)  # a backchannel does not take the floor
    return offsets, backchannels


def summary(offsets, backchannels, minutes):
    v = np.array(offsets)
    if not len(v):
        return {}
    return {"floor_takes": int(len(v)), "median_s": round(float(np.median(v)), 3),
            "iqr_s": [round(float(np.percentile(v, 25)), 3), round(float(np.percentile(v, 75)), 3)],
            "overlap_start_pct": round(100 * float(np.mean(v < 0)), 1),
            "over_1s_pct": round(100 * float(np.mean(v > 1)), 1),
            "backchannels_per_min": round(backchannels / minutes, 2) if minutes else None}


def main():
    root = Path(sys.argv[1])
    pooled = {"ai": ([], 0), "human": ([], 0)}
    minutes, calls = 0.0, {}
    for f in sorted(root.glob("*-speaker1.*")):
        name = f.name.rsplit("-speaker1", 1)[0]
        g = next(root.glob(f"{name}-speaker2.*"), None)
        if not g:
            continue
        try:
            x1, x2 = pcm(f), pcm(g)
        except subprocess.CalledProcessError:
            continue
        floor = lambda x: np.percentile(np.abs(x[: len(x) // FRAME * FRAME]).reshape(-1, FRAME).mean(1), 10)
        xa, xh = (x1, x2) if floor(x1) < floor(x2) else (x2, x1)
        sa, sh = spurts(xa), spurts(xh)
        m = max(len(xa), len(xh)) / 16000 / 60
        oa, ba = timing(sa, sh)
        oh, bh = timing(sh, sa)
        calls[name] = {"minutes": round(m, 2), "ai": summary(oa, ba, m), "human": summary(oh, bh, m)}
        minutes += m
        pooled["ai"] = (pooled["ai"][0] + oa, pooled["ai"][1] + ba)
        pooled["human"] = (pooled["human"][0] + oh, pooled["human"][1] + bh)
    print(json.dumps({"calls": len(calls), "minutes": round(minutes, 1),
                      "ai": summary(*pooled["ai"], minutes), "human": summary(*pooled["human"], minutes),
                      "per_call": calls}, indent=1))


if __name__ == "__main__":
    main()
