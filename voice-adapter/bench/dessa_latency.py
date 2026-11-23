"""Where does a Grok floor-take's gap go? (overnight hill-climb backlog 0d)

For every floor-take dessa_score finds in a Dessa-replay run (recorder WAVs), this lines up the call's trace and splits
the gap the phone hears into stages:
  human end -> speech_stopped   uplink + Grok's VAD silence timer (rx of input_audio_buffer.speech_stopped)
  speech_stopped -> created     Grok deciding to answer (rx of response.created)
  created -> first delta        generation to first audio (rx of the response's first response.output_audio.delta)
  first delta -> out frame      our output path to a voiced frame leaving the adapter (audio.on dir=output)
  out frame -> heard            WebRTC downlink + recorder (the WAV's AI onset)
The WAV timeline maps onto trace time by one constant per call: the mode of (adapter input audio.on - WAV human spurt
start), refined by the median of matched pairs; the residual spread is reported as align_mad_s.
Needs traces with grok.timing events (grok_leg.py, 2026-11-20 on).

  python dessa_latency.py RUN_DIR   -> one JSON object: per-stage median/IQR (s), takes matched, per-call alignment
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dessa_score as ds  # noqa: E402
import dessa_timing as dt  # noqa: E402

STAGES = ("human_end->speech_stopped", "speech_stopped->created", "created->first_delta",
          "first_delta->out_frame", "out_frame->heard")
# With LABEL/NAME-clock.json (dessa_replay, 11-20 on) the replay's sender and recorder share the trace's clock, so the
# one-way legs are measured, not inferred: out_frame->heard above equals uplink + downlink, because the alignment
# constant c is fitted on input onsets and so already contains the uplink.
CLOCK_STAGES = ("downlink(out_frame->recorder arrival)", "wav_placement(wav onset - recorder arrival)")
UPLINK = "uplink(human onset->adapter input)"  # per human spurt, not per take


def load_trace(path: Path):
    evs = [json.loads(line) for line in path.open(errors="replace") if line.strip()]
    inputs = [e["t"] for e in evs if e.get("ev") == "audio.on" and e.get("dir") == "input"]
    outputs = [e["t"] for e in evs if e.get("ev") == "audio.on" and e.get("dir") == "output"]
    timing = [e for e in evs if e.get("ev") == "grok.timing"]
    wall0 = float(np.median([e["wall"] - e["t"] for e in evs if "wall" in e])) if evs else None
    return inputs, outputs, timing, wall0


def first_in(xs, lo, hi):
    return next((x for x in sorted(xs) if lo <= x <= hi), None)


def lags(starts, xs, lo, hi):
    """For each start a, (first x in [a+lo, a+hi]) - a; starts with no match are dropped."""
    hits = ((a, first_in(xs, a + lo, a + hi)) for a in starts)
    return [b - a for a, b in hits if b is not None]


def align(trace_on, wav_starts):
    """Constant c with trace_t = wav_t + c, from input onsets. -> (c, median abs deviation of matched pairs)."""
    d = np.array([t - s for t in trace_on for s in wav_starts if -2 < t - s < 10])
    if not len(d):
        return None, None
    hist, edges = np.histogram(d, bins=np.arange(-2, 10.02, 0.02))
    c = edges[int(np.argmax(hist))] + 0.01
    near = d[np.abs(d - c) < 0.15]
    c = float(np.median(near))
    return c, float(np.median(np.abs(near - c)))


def last(xs, lo, hi):
    hit = [x for x in xs if lo <= x <= hi]
    return hit[-1] if hit else None


def decompose(run: Path):
    traces = sorted((run / "traces").rglob("*.jsonl"))
    names = [n for n in ds.CALLS if (run / f"{n}-ai.wav").exists()]
    if len(traces) != len(names):
        raise SystemExit(f"{run}: {len(traces)} traces for {len(names)} replayed calls; cannot pair them")
    rows, per_call, legs = [], {}, {k: [] for k in (*CLOCK_STAGES, UPLINK)}
    for name, tpath in zip(names, traces):
        xa, xh = ds.wav16(run / f"{name}-ai.wav"), ds.wav16(run / f"{name}-human.wav")
        sa, sh = dt.spurts(xa), dt.spurts(xh)
        takes, _ = ds.events(sa, sh)
        inputs, outputs, timing, wall0 = load_trace(tpath)
        c, mad = align(inputs, [s for s, _ in sh])
        per_call[name] = {"trace": tpath.name, "align_s": None if c is None else round(c, 3),
                          "align_mad_s": None if mad is None else round(mad, 3), "takes": len(takes)}
        clock_path = run / f"{name}-clock.json"
        clock = json.loads(clock_path.read_text()) if clock_path.exists() and wall0 is not None else None
        if clock:  # trace t of WAV sample 0; c - c_clock is the uplink the input-onset fit absorbed
            c_clock = clock["sender_wall"] - wall0
            heard_t = [w - wall0 for w in clock["heard_on_wall"]]
            legs[UPLINK] += lags([s + c_clock for s, _ in sh], inputs, -0.05, 1.0)
            legs[CLOCK_STAGES[0]] += lags(outputs, heard_t, -0.05, 1.0)
            # > 0: the WAV places AI speech later than the recorder received it
            legs[CLOCK_STAGES[1]] += [-d for d in lags([s + c_clock for s, _ in sa], heard_t, -0.3, 0.3)]
            per_call[name] |= {"align_clock_s": round(c_clock, 3)}
        if c is None:
            continue
        stopped = [e["t"] for e in timing if e["type"] == "input_audio_buffer.speech_stopped"]
        created = {e["response_id"]: e["t"] for e in timing if e["type"] == "response.created"}
        first = {e["response_id"]: e["t"] for e in timing if e["type"] == "response.output_audio.delta"}
        for s, off, _ in takes:
            he, heard = s - off + c, s + c
            ts = last(stopped, he - 0.3, heard)
            rid = max((r for r, t in first.items() if t <= heard + 0.05), key=lambda r: first[r], default=None)
            tf = first.get(rid)
            tc = created.get(rid)
            to = next((t for t in outputs if tf is not None and tf - 0.02 <= t <= heard + 0.3), None)
            marks = [he, ts, tc, tf, to, heard]
            row = {"call": name, "at_s": round(s, 2), "gap_s": round(off, 3)}
            for k, stage in enumerate(STAGES):
                a, b = marks[k], marks[k + 1]
                row[stage] = None if a is None or b is None else round(b - a, 3)
            rows.append(row)
    stats = {}
    for stage in STAGES:
        v = np.array([r[stage] for r in rows if r[stage] is not None])
        stats[stage] = {"n": int(len(v))} | ({"median_s": round(float(np.median(v)), 3),
                                               "iqr_s": [round(float(np.percentile(v, 25)), 3),
                                                         round(float(np.percentile(v, 75)), 3)]} if len(v) else {})
    for stage, v in legs.items():  # per onset, every engine (no grok.timing needed)
        if v:
            stats[stage] = {"n": int(len(v)), "median_s": round(float(np.median(v)), 3),
                            "iqr_s": [round(float(np.percentile(v, 25)), 3), round(float(np.percentile(v, 75)), 3)]}
    full = [r for r in rows if all(r[st] is not None for st in STAGES)]
    return {"run": run.name, "takes": len(rows), "fully_matched": len(full),
            "gap_median_s": round(float(np.median([r["gap_s"] for r in rows])), 3) if rows else None,
            "stages": stats, "per_call": per_call, "rows": rows}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(json.dumps(decompose(Path(arg))))
