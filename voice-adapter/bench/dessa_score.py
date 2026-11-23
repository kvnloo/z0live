"""Score a Dessa-replay run with confidence intervals and Dessa-labelled cut-ins (overnight hill-climb backlog 0a-c).

dessa_replay.py writes NAME-ai.wav / NAME-human.wav (48 kHz, aligned, human starts LEAD_S into the file). This reads
those WAVs and adds what one D number cannot show:
  - D and every component with a 95% block-bootstrap CI (10 s blocks pooled across calls, 2000 resamples), so two
    runs can be compared against noise;
  - false cut-in rate: AI floor-takes after a human talkspurt that Dessa did NOT answer (the human track is the same,
    so Dessa's own AI track labels which pauses were safe to take); missed-take rate: human talkspurts Dessa answered
    and we did not;
  - AI talkspurt durations, spurts under 0.5 s counted apart (clipped blips can score as fast floor-takes);
  - delegation.created events in the run's traces (near zero -> lane hypotheses cannot move this bench).

  python dessa_score.py RUN_DIR [RUN_DIR ...]     -> one JSON object per run dir on stdout
  python dessa_score.py --dessa                   -> Dessa scored against itself on the same clips (sanity check)
  python dessa_score.py --diff RUN_A RUN_B        -> D(B) - D(A) with a paired block-bootstrap 95% CI
Timing rules are dessa_timing's (frozen); the floor-take offsets here are asserted equal to dessa_timing.timing's.
"""
import json
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dessa_timing as dt  # noqa: E402

REF = Path.home() / ".local/share/z0live/dessa-ref/raw-20261119"
CALLS = ("coffee-ding", "ai-release", "anniversary", "dc-comics")  # dessa_replay.DEFAULT_CALLS
LEAD_S, CLIP_S, BLOCK_S, N_BOOT = 2.0, 60.0, 10.0, 2000
TARGET = {"median_s": 0.43, "over_1s_pct": 13.2, "backchannels_per_min": 3.5}


def score_d(median, over1, bc):
    return abs(median - TARGET["median_s"]) + max(0.0, over1 - TARGET["over_1s_pct"]) / 100 \
        + abs(bc - TARGET["backchannels_per_min"]) / 10


def events(a, b):
    """dessa_timing.timing, keeping for each floor-take the index of b's talkspurt it answers.
    -> (takes [(start, offset, b_index)], backchannel starts [s])."""
    ev = sorted([(s, e, "a", -1) for s, e in a] + [(s, e, "b", i) for i, (s, e) in enumerate(b)])
    takes, bcs, prev = [], [], None
    for s, e, who, i in ev:
        inside = prev is not None and prev[2] == "b" and prev[0] <= s and e <= prev[1]
        if who == "a" and prev is not None and prev[2] == "b":
            if inside:
                bcs.append(s)
            else:
                takes.append((s, s - prev[1], prev[3]))
        if who == "b" or not inside:
            prev = (s, e, who, i)
    return takes, bcs


def dessa_ai(name):
    """Dessa's AI talkspurts on the replay timeline (shifted by LEAD_S, clipped to the replayed minute)."""
    x = [dt.pcm(p) for p in sorted(REF.glob(f"{name}-speaker*.*"))]
    floor = lambda v: np.percentile(np.abs(v[: len(v) // dt.FRAME * dt.FRAME]).reshape(-1, dt.FRAME).mean(1), 10)
    ai, human = (x[0], x[1]) if floor(x[0]) < floor(x[1]) else (x[1], x[0])
    n = int(16000 * CLIP_S)
    return dt.spurts(ai[:n]), dt.spurts(human[:n])


def shifted(sp, by):
    return [(s + by, e + by) for s, e in sp]


def wav16(path):
    """The replay's own 48 -> 16 kHz decimation (dessa_replay.replay), so offsets match its receipt exactly."""
    with wave.open(str(path)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return x[::3].astype(np.float32) / 32768.0


def per_call(name, sa, sh, sd):
    """sa: our AI spurts, sh: human spurts, sd: Dessa's spurts, all on one timeline."""
    takes, bcs = events(sa, sh)
    assert [round(o, 6) for _, o, _ in takes] == [round(o, 6) for o in dt.timing(sa, sh)[0]], name
    dtakes, _ = events(sd, sh)
    answered = {i for _, _, i in dtakes}
    ours = {i for _, _, i in takes}
    durs = [e - s for s, e in sa]
    return {"takes": [(s, o, i not in answered) for s, o, i in takes], "bcs": bcs,
            "human_spurts": len(sh), "dessa_answered": len(answered), "missed": len(answered - ours),
            "durs": durs}


def pooled(calls, minutes):
    offs = np.array([o for c in calls for _, o, _ in c["takes"]])
    bc = sum(len(c["bcs"]) for c in calls)
    if not len(offs):
        return None
    med, over1, bcm = float(np.median(offs)), 100 * float(np.mean(offs > 1)), bc / minutes
    return med, over1, bcm, score_d(med, over1, bcm)


def bootstrap(calls, rng):
    """95% CI of (median, waits>1s %, bc/min, D) resampling 10 s blocks of every call's timeline."""
    blocks = []
    for c in calls:
        for k in range(int(np.ceil((LEAD_S + CLIP_S) / BLOCK_S))):
            lo, hi = k * BLOCK_S, (k + 1) * BLOCK_S
            blocks.append(([o for s, o, _ in c["takes"] if lo <= s < hi], sum(lo <= s < hi for s in c["bcs"])))
    minutes_per_block = BLOCK_S / 60
    out = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(blocks), len(blocks))
        offs = np.array([o for j in pick for o in blocks[j][0]])
        if not len(offs):
            continue
        med, over1 = float(np.median(offs)), 100 * float(np.mean(offs > 1))
        bcm = sum(blocks[j][1] for j in pick) / (len(pick) * minutes_per_block)
        out.append((med, over1, bcm, score_d(med, over1, bcm)))
    a = np.array(out)
    return [[round(float(np.percentile(a[:, k], 2.5)), 3), round(float(np.percentile(a[:, k], 97.5)), 3)]
            for k in range(4)]


def traces_delegations(run):
    n = 0
    for f in (run / "traces").rglob("*.jsonl") if (run / "traces").exists() else []:
        n += sum('"delegation.created"' in line for line in f.open(errors="replace"))
    return n


def report(label, calls, minutes, delegations=None):
    p = pooled(calls, minutes)
    if p is None:
        return {"run": label, "error": "no floor-takes"}
    takes = [t for c in calls for t in c["takes"]]
    durs = np.array([d for c in calls for d in c["durs"]])
    ci = bootstrap(calls, np.random.default_rng(0))
    dessa_answered = sum(c["dessa_answered"] for c in calls)
    return {
        "run": label, "calls": len(calls), "minutes": round(minutes, 2), "floor_takes": len(takes),
        "D": round(p[3], 3), "D_ci95": ci[3],
        "median_s": round(p[0], 3), "median_ci95": ci[0],
        "over_1s_pct": round(p[1], 1), "over_1s_ci95": ci[1],
        "backchannels_per_min": round(p[2], 2), "bc_ci95": ci[2],
        "false_cut_in_pct": round(100 * sum(f for _, _, f in takes) / len(takes), 1),
        "missed_take_pct": round(100 * sum(c["missed"] for c in calls) / dessa_answered, 1) if dessa_answered else None,
        "spurts": int(len(durs)), "spurt_median_s": round(float(np.median(durs)), 2) if len(durs) else None,
        "spurts_lt_0_5s": int(np.sum(durs < 0.5)), "ai_talk_s": round(float(durs.sum()), 1),
        "delegations": delegations,
    }


def score_run(run: Path):
    calls, minutes = load_calls(run)
    return report(run.name, calls, minutes, traces_delegations(run))


def score_dessa():
    calls, minutes = [], 0.0
    for name in CALLS:
        sd, sh = dessa_ai(name)
        calls.append(per_call(name, sd, sh, sd))
        minutes += CLIP_S / 60
    return report("dessa-self", calls, minutes)


def load_calls(run: Path):
    """Every replayed call in RUN (a call that errored has no WAVs and is skipped) -> (calls, minutes)."""
    calls, minutes = [], 0.0
    for name in CALLS:
        fa, fh = run / f"{name}-ai.wav", run / f"{name}-human.wav"
        if not (fa.exists() and fh.exists()):
            continue
        xa, xh = wav16(fa), wav16(fh)
        sd, _ = dessa_ai(name)
        calls.append(per_call(name, dt.spurts(xa), dt.spurts(xh), shifted(sd, LEAD_S)))
        minutes += len(xh) / 16000 / 60
    return calls, minutes


def diff(run_a: Path, run_b: Path):
    """D(b) - D(a) with a PAIRED block bootstrap: both runs replay the same human timeline, so block k of one run
    is resampled together with block k of the other. A CI that excludes 0 is a real change."""
    (ca, _), (cb, _) = load_calls(run_a), load_calls(run_b)
    assert len(ca) == len(cb), "paired diff needs the same calls in both runs"
    nblk = int(np.ceil((LEAD_S + CLIP_S) / BLOCK_S))

    def blocks(calls):
        return [([o for s, o, _ in c["takes"] if k * BLOCK_S <= s < (k + 1) * BLOCK_S],
                 sum(k * BLOCK_S <= s < (k + 1) * BLOCK_S for s in c["bcs"])) for c in calls for k in range(nblk)]

    ba, bb = blocks(ca), blocks(cb)

    def d_of(bl, pick):
        offs = np.array([o for j in pick for o in bl[j][0]])
        if not len(offs):
            return None
        return score_d(float(np.median(offs)), 100 * float(np.mean(offs > 1)),
                       sum(bl[j][1] for j in pick) / (len(pick) * BLOCK_S / 60))

    rng, deltas = np.random.default_rng(0), []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(ba), len(ba))
        da, db = d_of(ba, pick), d_of(bb, pick)
        if da is not None and db is not None:
            deltas.append(db - da)
    full = list(range(len(ba)))
    return {"a": run_a.name, "b": run_b.name, "dD": round(d_of(bb, full) - d_of(ba, full), 3),
            "dD_ci95": [round(float(np.percentile(deltas, 2.5)), 3), round(float(np.percentile(deltas, 97.5)), 3)]}


def main():
    if sys.argv[1:] == ["--dessa"]:
        print(json.dumps(score_dessa()))
        return
    if sys.argv[1:2] == ["--diff"]:
        print(json.dumps(diff(Path(sys.argv[2]), Path(sys.argv[3]))))
        return
    for arg in sys.argv[1:]:
        print(json.dumps(score_run(Path(arg))))


if __name__ == "__main__":
    main()
