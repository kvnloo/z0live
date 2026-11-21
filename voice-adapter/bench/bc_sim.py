"""Offline check of a backchannel trigger rule (hyp 4) on recorded replay runs, before it touches a call.

For every NAME-human.wav / NAME-ai.wav pair in the run dirs, replay the human track through the same 20 ms peak
rule the adapter uses (Level: peak >= LEVEL_ON is sound), fire a clip when the human has talked >= --talk s since
their span began (or since the last clip), has just gone quiet for --quiet s, and the AI track is silent; then paste
a --clip-s long burst into the AI track and score both tracks with dessa_timing. Reports how many clips land as
backchannels (inside the human's talkspurt) vs floor-takes (the human had stopped: a false "mm-hm" at a turn end).

  python bench/bc_sim.py RUN_DIR... [--quiet 0.06] [--talk 3] [--cool 6] [--clip-s 0.4] [--delay 0.25]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import dessa_timing as dt  # noqa: E402

LEVEL_ON = 800 / 32768  # mobile_live.LEVEL_ON as a float peak
LEVEL_OFF = 0.3         # mobile_live.LEVEL_OFF_SECONDS
F = 320                 # 20 ms at 16 kHz


def triggers(human, ai, quiet, talk, cool):
    n = len(human) // F
    hp = np.abs(human[: n * F]).reshape(n, F).max(axis=1)
    ap = np.abs(ai[: n * F]).reshape(n, F).max(axis=1)
    out, on, q, last, since = [], None, None, -1e9, None
    for i in range(n):
        t = i * 0.02
        if hp[i] >= LEVEL_ON:
            on = t if on is None else on
            since = t if since is None else since
            q = None
            continue
        if on is None:
            continue
        q = t if q is None else q
        if t - q >= LEVEL_OFF:
            on, q, since = None, None, None
            continue
        ai_quiet = ap[max(0, i - 25): i + 1].max() < LEVEL_ON  # AI silent for the last 0.5 s
        if (since is not None and t - q >= quiet and q - since >= talk and t - last >= cool and ai_quiet):
            out.append(t)
            last, since = t, None  # restart the talk clock after a clip
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="+")
    p.add_argument("--quiet", type=float, default=0.06)
    p.add_argument("--talk", type=float, default=3.0)
    p.add_argument("--cool", type=float, default=6.0)
    p.add_argument("--clip-s", type=float, default=0.4)
    # Pause start -> clip start on the recorder: the human's uplink to the adapter (~0.12 s, A5) + the trigger's quiet
    # window + adapter -> recorder (~0.13 s, A10 bc_check). With 0.29 the sim reproduces A10's live landing mix
    # (more floor-takes than backchannels); with 0 it overstates the gain ~3x.
    p.add_argument("--delay", type=float, default=0.29)
    a = p.parse_args()
    tot = {"clips": 0, "bc_before": 0, "bc_after": 0, "takes_before": 0, "takes_after": 0, "minutes": 0.0}
    for run in a.runs:
        for hw in sorted(Path(run).glob("*-human.wav")):
            aw = hw.with_name(hw.name.replace("-human", "-ai"))
            h, x = dt.pcm(hw), dt.pcm(aw)
            m = min(len(h), len(x)); h, x = h[:m], x[:m]
            ts = triggers(h, x, a.quiet, a.talk, a.cool)
            y = x.copy()
            for t in ts:  # a clip-shaped burst at a voiced level (-20 dBFS noise), enough for the spurt detector
                s = int((t + a.delay) * 16000); e = min(m, s + int(a.clip_s * 16000))
                y[s:e] += 0.1 * np.random.default_rng(int(t * 1000)).standard_normal(e - s).astype(np.float32)
            hs = dt.spurts(h)
            ob, bb = dt.timing(dt.spurts(x), hs)
            oa, ba = dt.timing(dt.spurts(y), hs)
            tot["clips"] += len(ts); tot["bc_before"] += bb; tot["bc_after"] += ba
            tot["takes_before"] += len(ob); tot["takes_after"] += len(oa); tot["minutes"] += m / 16000 / 60
            print(f"{hw.parent.name}/{hw.name.split('-human')[0]}: clips={len(ts)} bc {bb}->{ba} takes {len(ob)}->{len(oa)}")
    t = tot
    print(f"TOTAL clips={t['clips']} bc {t['bc_before']}->{t['bc_after']} (+{t['bc_after'] - t['bc_before']}) "
          f"takes {t['takes_before']}->{t['takes_after']} (+{t['takes_after'] - t['takes_before']} false takes) "
          f"bc/min {t['bc_before'] / t['minutes']:.2f}->{t['bc_after'] / t['minutes']:.2f}")


if __name__ == "__main__":
    main()
