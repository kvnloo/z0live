"""Floor decision points from the Dessa reference calls (Track C in docs/HILLCLIMB-OVERNIGHT.md).

Every 20 ms the policy decides from the human audio heard so far; its sound reaches the ear DELAY later. A frame's
label is what Dessa's own AI track did there, by dessa_timing's rules: a floor-take (an AI talkspurt that starts after
the human spoke last) or a backchannel (an AI talkspurt entirely inside a human talkspurt). Everything else is wait.

Dessa audio is private: features and labels are cached under ~/.local/share/z0live/orch, never in git.

  python bench/floor_data.py      -> builds the cache, prints per-call decision-point counts
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dessa_timing as dt  # noqa: E402
from guest_floor import substantive  # noqa: E402

REF = Path.home() / ".local/share/z0live/dessa-ref/raw-20261119"
CACHE = Path.home() / ".local/share/z0live/orch/cache"
ASR = Path.home() / ".local/share/z0live/orch/asr"  # floor_asr.py output
HOP = 0.02                # decision frame (the adapter's 20 ms audio frame)
LEVEL_ON = 800 / 32768    # mobile_live.LEVEL_ON as a float peak
BRIDGE = 18               # dessa_timing: pauses under 180 ms do not split a talkspurt (10 ms frames)
TAIL = 30                 # prosody of the last 0.3 s of voiced audio
FEATURES = ("e_now", "e_max100", "quiet_s", "lquiet_s", "run_s", "prev_gap_s", "vfrac_2s", "vfrac_8s", "runs_8s",
            "e_tail", "f0_tail", "f0_slope", "pfrac_tail", "log_quiet", "e_last")
# A talkspurt's transcript exists ASR_LAG after it ends (inferred: the local-voice smoke's LFM2.5-Audio on Metal
# encoded a slice in 33-44 ms and decoded it in ~20-70 ms).
ASR_LAG = 0.15
TEXT_FEATURES = ("t_words", "t_open", "t_question", "t_final", "t_ack", "t_fresh")
OPEN = frozenset("and but so or because cause if when that which who the a an to of for with in on at from about my "
                 "your his her their our i you we they it is are was were be been have has had do does did can could "
                 "would will um uh like just than then also".split())
QWORDS = frozenset("what why how where when who which do does did is are can could would will should have has".split())


@dataclass
class Reply:
    """One Dessa floor-take and the AI talkspurts that continue it before the human next takes the floor."""
    answers: int                                   # index of the human talkspurt it follows
    spurts: list = field(default_factory=list)     # [(s, e)], first one is the take

    @property
    def onset(self):
        return self.spurts[0][0]

    @property
    def end(self):
        return self.spurts[-1][1]


@dataclass
class Call:
    name: str
    seconds: float
    human: list               # human talkspurts [(s, e)] (dessa_timing.spurts)
    dessa: list               # Dessa AI talkspurts
    X: np.ndarray             # (frames, FEATURES) causal human-audio features; frame i decides at t = (i + 1) * HOP
    last_voiced: np.ndarray   # (frames,) end of the last voiced 10 ms frame heard by t (-1 = none yet)
    T: np.ndarray | None = None  # (frames, TEXT_FEATURES) from the newest transcript available at t; None = no ASR

    @property
    def t(self):
        return (np.arange(len(self.X)) + 1) * HOP


@dataclass
class Script:
    """Dessa's AI track split the way dessa_timing scores it."""
    opening: list   # AI talkspurts before the human first spoke
    replies: list   # [Reply]
    bcs: list       # backchannel talkspurts [(s, e)]


def names():
    out = []
    for f in sorted(REF.glob("*-speaker1.flac")):
        name = f.name.rsplit("-speaker1", 1)[0]
        g = REF / f"{name}-speaker2.flac"
        if g.exists() and min(f.stat().st_size, g.stat().st_size) > 1000:  # more-cs-fight is a 9-byte stub
            out.append(name)
    return out


def tracks(name):
    """(human, ai) 16 kHz float32: the AI track has the quieter floor (dessa_timing.main)."""
    x = [dt.pcm(REF / f"{name}-speaker{k}.flac") for k in (1, 2)]
    floor = lambda v: np.percentile(np.abs(v[: len(v) // dt.FRAME * dt.FRAME]).reshape(-1, dt.FRAME).mean(1), 10)
    return (x[1], x[0]) if floor(x[0]) < floor(x[1]) else (x[0], x[1])


def script(human, dessa):
    """dessa_timing.timing's event walk, keeping which AI talkspurt is a take, a continuation or a backchannel."""
    ev = sorted([(s, e, "a", -1) for s, e in dessa] + [(s, e, "h", j) for j, (s, e) in enumerate(human)])
    opening, replies, bcs, prev = [], [], [], None
    for s, e, who, j in ev:
        inside = prev is not None and prev[2] == "h" and prev[0] <= s and e <= prev[1]
        if who == "a":
            if prev is not None and prev[2] == "h":
                if inside:
                    bcs.append((s, e))
                else:
                    replies.append(Reply(prev[3], [(s, e)]))
            elif replies:
                replies[-1].spurts.append((s, e))
            else:
                opening.append((s, e))
        if who == "h" or not inside:
            prev = (s, e, who, j)
    return Script(opening, replies, bcs)


def _windows(x, n, width, hop):
    """Causal analysis windows: window j ends at sample (j + 1) * hop."""
    pad = np.concatenate([np.zeros(width - hop, np.float32), x[: n * hop]])
    return np.lib.stride_tricks.sliding_window_view(pad, width)[::hop][:n]


def pitch(x, n):
    """Semitones re 100 Hz per 10 ms frame (40 ms causal window, autocorrelation, 70-400 Hz) and a periodicity mask."""
    st, per = np.zeros(n), np.zeros(n, bool)
    win = np.hanning(640).astype(np.float32)
    for a in range(0, n, 20000):
        w = _windows(x, n, 640, 160)[a: a + 20000] * win
        ac = np.fft.irfft(np.abs(np.fft.rfft(w, 1024)) ** 2, 1024)[:, :240]
        lag = 40 + np.argmax(ac[:, 40:229], axis=1)
        r = ac[np.arange(len(ac)), lag] / (ac[:, 0] + 1e-9)
        st[a: a + len(w)] = 12 * np.log2(16000 / lag / 100)
        per[a: a + len(w)] = r > 0.5
    return st, per


def features(x):
    """Human audio -> (X, last_voiced) at 20 ms decisions. Voiced is dessa_timing's rule (18 dB over the track's
    10th-percentile floor; the floor is one per-call constant, the only non-causal input)."""
    n = len(x) // dt.FRAME
    fr = x[: n * dt.FRAME].reshape(n, dt.FRAME)
    db = 20 * np.log10(np.sqrt(np.mean(fr ** 2, axis=1) + 1e-12))
    floor = np.percentile(db, 10)
    voiced = (db > floor + 18) & (db > -50)
    rel = np.clip(db - floor, -10, 60)
    loud = np.abs(fr).max(axis=1) >= LEVEL_ON
    st, per = pitch(x, n)
    per &= voiced
    k = np.arange(n, dtype=np.float64)

    def last(mask):  # index of the last True frame at or before each frame, -1 if none
        return np.maximum.accumulate(np.where(mask, np.arange(n), -1))

    lv, ll = last(voiced), last(loud)
    idx = np.flatnonzero(voiced)
    gaps = np.diff(idx) - 1
    new = np.r_[True, gaps >= BRIDGE]
    run_start = idx[new][np.cumsum(new) - 1]                 # per voiced frame
    run_gap = np.r_[np.inf, gaps][new][np.cumsum(new) - 1]
    pos = np.searchsorted(idx, np.maximum(lv, 0))            # lv -> position in idx
    rs = np.where(lv >= 0, run_start[np.minimum(pos, len(idx) - 1)], 0)
    pg = np.where(lv >= 0, run_gap[np.minimum(pos, len(idx) - 1)], np.inf)
    starts = np.zeros(n, bool)
    starts[run_start] = True

    def csum(v):
        return np.r_[0.0, np.cumsum(v, dtype=np.float64)]  # C[b + 1] - C[a] = sum over frames a..b

    Cv, Ce, Cp, Cs = csum(voiced), csum(voiced * rel), csum(per), csum(per * st)
    Ck, Ckk, Cks = csum(per * k), csum(per * k * k), csum(per * k * st)
    Cst = csum(starts)

    def span(C, a, b):
        return C[b + 1] - C[np.clip(a, 0, None)]

    b = np.maximum(lv, 0)
    ta = b - TAIL + 1
    nv_t, nv_r = span(Cv, ta, b), span(Cv, rs, b)
    np_t, np_r = span(Cp, ta, b), span(Cp, rs, b)
    e_tail = span(Ce, ta, b) / np.maximum(nv_t, 1) - span(Ce, rs, b) / np.maximum(nv_r, 1)
    f0_tail = np.where((np_t > 2) & (np_r > 2), span(Cs, ta, b) / np.maximum(np_t, 1) - span(Cs, rs, b)
                       / np.maximum(np_r, 1), 0.0)
    sk, skk, sks, ss = span(Ck, ta, b), span(Ckk, ta, b), span(Cks, ta, b), span(Cs, ta, b)
    den = np_t * skk - sk ** 2
    f0_slope = np.where((np_t >= 4) & (den > 0), (np_t * sks - sk * ss) / np.where(den > 0, den, 1) * 100, 0.0)

    j = np.arange(1, n, 2)  # the 10 ms frame that closes each 20 ms decision frame
    t = (j + 1) * 0.01
    quiet = np.where(lv[j] >= 0, t - (lv[j] + 1) * 0.01, 3.0)
    lquiet = np.where(ll[j] >= 0, t - (ll[j] + 1) * 0.01, 3.0)
    run_s = np.where(lv[j] >= 0, (lv[j] + 1 - rs[j]) * 0.01, 0.0)
    vfrac = lambda w: (Cv[j + 1] - Cv[np.clip(j + 1 - w, 0, None)]) / w
    runs_8s = Cst[j + 1] - Cst[np.clip(j + 1 - 800, 0, None)]
    e_max = np.maximum.reduce([rel[np.clip(j - d, 0, None)] for d in range(10)])
    e_last = np.where(lv[j] >= 0, rel[b[j]] - span(Ce, rs, b)[j] / np.maximum(nv_r[j], 1), 0.0)
    X = np.stack([
        np.maximum(rel[j], rel[j - 1]), e_max, np.minimum(quiet, 3), np.minimum(lquiet, 3), np.minimum(run_s, 20),
        np.minimum(pg[j], 5), vfrac(200), vfrac(800), runs_8s, e_tail[j], np.clip(f0_tail[j], -12, 12),
        np.clip(f0_slope[j], -60, 60), np_t[j] / np.maximum(nv_t[j], 1), np.log(np.minimum(quiet, 3) + 0.02), e_last,
    ], axis=1).astype(np.float32)
    last_voiced = np.where(lv[j] >= 0, (lv[j] + 1) * 0.01, -1.0)
    return X, last_voiced


def load(name):
    path = CACHE / f"{name}.npz"
    if not path.exists():
        human, ai = tracks(name)
        X, last_voiced = features(human)
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        np.savez(path, X=X, last_voiced=last_voiced, human=np.array(dt.spurts(human)).reshape(-1, 2),
                 dessa=np.array(dt.spurts(ai)).reshape(-1, 2), seconds=max(len(human), len(ai)) / 16000)
    z = np.load(path)
    c = Call(name, float(z["seconds"]), [tuple(p) for p in z["human"].tolist()],
             [tuple(p) for p in z["dessa"].tolist()], z["X"], z["last_voiced"])
    c.T = text_features(c)
    return c


def text_row(text):
    w = re.findall(r"[a-z']+", text.lower())
    if not w:
        return [0.0] * 5
    end = text.rstrip()[-1:]
    return [np.log1p(len(w)), float(w[-1] in OPEN), float(w[0] in QWORDS or end == "?"), float(end in ".!?"),
            float(not substantive(text))]


def text_features(call):
    """Per decision frame, the newest talkspurt transcript available (talkspurt end + ASR_LAG <= t): its length, an
    open last word (conjunction, article, filler...), a question, final punctuation, a bare acknowledgement, and
    whether it covers the latest speech heard (t_fresh)."""
    path = ASR / f"{call.name}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text())
    assert [(s, e) for s, e, _ in rows] == [tuple(p) for p in call.human], call.name
    ready = np.array([e + ASR_LAG for _, e, _ in rows])
    feats = np.array([text_row(r[2]) for r in rows]).reshape(-1, 5)
    k = np.searchsorted(ready, call.t, side="right") - 1
    T = np.where(k[:, None] >= 0, feats[np.maximum(k, 0)], 0.0)
    fresh = (k >= 0) & (np.array([e for _, e, _ in rows])[np.maximum(k, 0)] >= call.last_voiced - 0.2)
    return np.hstack([T, fresh[:, None]]).astype(np.float32)


def labels(call, sc, delay):
    """Per decision frame: (take, take_eligible, bc, bc_eligible) from Dessa's track.
    A Dessa sound at onset s was decided at s - delay (positive frames: 40 ms before to 20 ms after). Between that
    decision and the end of what it started, the AI holds the floor: those frames are not decisions. A take is
    eligible once the human has spoken since the AI's last reply ended; a backchannel while the human is talking."""
    t = call.t
    take, bc, busy = (np.zeros(len(t), bool) for _ in range(3))

    def mark(onset, end, pos):
        d = onset - delay
        pos |= (t > d - 0.04) & (t <= d + 0.02)
        busy[(t > d + 0.02) & (t <= end)] = True

    for r in sc.replies:
        mark(r.onset, r.end, take)
    for s, e in sc.bcs:
        mark(s, e, bc)
    for s, e in sc.opening:
        busy[(t > s - delay) & (t <= e)] = True
    ends = np.array(sorted([r.end for r in sc.replies] + [e for _, e in sc.opening]) or [-1.0])
    k = np.searchsorted(ends, t, side="right") - 1
    ai_end = np.where(k >= 0, ends[np.maximum(k, 0)], -1.0)
    quiet = call.X[:, FEATURES.index("quiet_s")]
    take_ok = (~busy & (call.last_voiced > ai_end)) | take
    bc_ok = (~busy & (quiet < 0.2) & (call.last_voiced > 0)) | bc
    return take, take_ok, bc, bc_ok


def main():
    tot = np.zeros(5)
    for name in names():
        c = load(name)
        sc = script(c.human, c.dessa)
        take, take_ok, bc, bc_ok = labels(c, sc, 0.25)
        row = np.array([c.seconds / 60, len(sc.replies), len(sc.bcs), take_ok.sum(), bc_ok.sum()])
        tot += row
        print(f"{name:32s} min {row[0]:5.2f} takes {len(sc.replies):3d} bcs {len(sc.bcs):3d} "
              f"take-decisions {int(row[3]):6d} bc-decisions {int(row[4]):6d} opening {len(sc.opening)}")
    print(f"TOTAL min {tot[0]:.1f} takes {int(tot[1])} bcs {int(tot[2])} take-decisions {int(tot[3])} "
          f"bc-decisions {int(tot[4])}")


if __name__ == "__main__":
    main()
