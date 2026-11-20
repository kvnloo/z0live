"""Floor decision points in real z0live call traces (Track C): event timings only, no transcript text is read.

A trace's Level spans (audio.on .. audio.off "at", peak >= LEVEL_ON, 0.3 s of quiet ends a span) give both sides of a
call on the adapter's clock: input = the caller after the uplink, output = the voice before the downlink. Every caller
span that ends while the voice is quiet is a decision point:
  shift  the voice starts before the caller does again (gap = voice on - caller off)
  hold   the caller starts again first (pause = caller on - caller off, >= 0.3 s by the Level rule)
A pure silence rule that takes the floor after T s of Level quiet cuts into every hold pause longer than T, so the
hold-pause distribution is the false cut-in curve of a timeout on this caller. Spans are Level spans, not
dessa_timing's audio rule (180 ms bridge, relative floor), so compare shapes with the Dessa bench, not decimals.

  python bench/floor_traces.py [TRACE_DIR]     -> table on stdout, JSON under ~/.local/share/z0live/orch/
  python bench/floor_traces.py --dessa         -> the same table for the Dessa calls, Level spans cut from the audio
                                                  (is the training data's caller pausing like the real caller?)
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dessa_timing as dt  # noqa: E402

TRACES = Path.home() / ".local/share/z0live/voice-traces"
OUT = Path.home() / ".local/share/z0live/orch/traces-floor.json"
THRESHOLDS = (0.3, 0.5, 0.7, 1.0, 1.5, 2.0)
FRESH_S = 120  # a trace written in the last 2 min may be a live call: skip it


def spans(path):
    """-> (engine, caller spans, voice spans) on the trace clock; muted caller spans are dropped."""
    engine, open_, out = None, {}, {"input": [], "output": []}
    for line in path.open(errors="replace"):
        if '"ev": "audio.' not in line and '"call.created"' not in line:
            continue  # skip provider events without parsing them (they carry transcript text)
        e = json.loads(line)
        if e["ev"] == "call.created":
            engine = e.get("engine")
        elif e["ev"] == "audio.on" and e.get("dir") in out:
            open_[e["dir"]] = None if e.get("muted") else e["t"]
        elif e["ev"] == "audio.off" and e.get("dir") in out and open_.get(e["dir"]) is not None:
            s = open_.pop(e["dir"])
            if e["at"] - s >= 0.09:  # dessa_timing drops bursts under 90 ms (mic clicks at the LEVEL_ON edge)
                out[e["dir"]].append((s, e["at"]))
    return engine, out["input"], out["output"]


def level_spans(x, off=0.3, on=800 / 32768):
    """mobile_live.Level on 16 kHz audio in 20 ms frames: on at the first loud frame, off where 0.3 s of quiet began."""
    n = len(x) // 320
    loud = np.abs(x[: n * 320]).reshape(n, 320).max(axis=1) >= on
    out, start, quiet = [], None, None
    for i, v in enumerate(loud):
        t = i * 0.02
        if v:
            start, quiet = (t if start is None else start), None
        elif start is not None:
            quiet = t if quiet is None else quiet
            if t - quiet >= off:
                if quiet - start >= 0.09:
                    out.append((start, quiet))
                start = quiet = None
    return out


def dessa_calls():
    """(name, engine, caller spans, voice spans) for every Dessa reference call."""
    import floor_data as fd
    for name in fd.names():
        human, ai = fd.tracks(name)
        yield name, "dessa", level_spans(human), level_spans(ai)


def trace_calls(root):
    for path in sorted(root.glob("*/*.jsonl")):
        if time.time() - path.stat().st_mtime >= FRESH_S:
            yield (path.stem[:6] + "-" + path.stem[-6:], *spans(path))


def points(caller, voice):
    """Decision points: (caller off, kind, seconds) for caller spans that end while the voice is quiet."""
    vs = np.array([s for s, _ in voice] + [np.inf])
    out = []
    for k, (_, off) in enumerate(caller):
        if any(s <= off < e for s, e in voice):
            continue  # the voice was talking: not a floor decision
        nxt_c = caller[k + 1][0] if k + 1 < len(caller) else np.inf
        nxt_v = vs[np.searchsorted(vs, off)]
        if nxt_v < nxt_c:
            out.append((off, "shift", nxt_v - off))
        elif nxt_c < np.inf:
            out.append((off, "hold", nxt_c - off))
    return out


def main():
    dessa = sys.argv[1:] == ["--dessa"]
    calls = dessa_calls() if dessa else trace_calls(Path(sys.argv[1]) if len(sys.argv) > 1 else TRACES)
    out_path = OUT.with_name("dessa-floor.json") if dessa else OUT
    rows, allp, offs, bcs, minutes = [], [], [], 0, 0.0
    for name, engine, caller, voice in calls:
        if len(caller) < 10 or len(voice) < 5:
            continue
        p = points(caller, voice)
        o, b = dt.timing(voice, caller)
        m = (max(e for _, e in caller + voice) - min(s for s, _ in caller + voice)) / 60
        shifts = [x for _, k, x in p if k == "shift"]
        holds = [x for _, k, x in p if k == "hold"]
        rows.append({"call": name, "engine": engine, "minutes": round(m, 1),
                     "shifts": len(shifts), "holds": len(holds),
                     "shift_gap_median_s": round(float(np.median(shifts)), 2) if shifts else None,
                     "hold_pause_median_s": round(float(np.median(holds)), 2) if holds else None})
        allp += p
        offs += o
        bcs += b
        minutes += m
    shifts = np.array([x for _, k, x in allp if k == "shift"])
    holds = np.array([x for _, k, x in allp if k == "hold"])
    o = np.array(offs)
    summary = {
        "calls": len(rows), "minutes": round(minutes, 1), "decision_points": len(allp),
        "shifts": int(len(shifts)), "holds": int(len(holds)),
        "shift_gap_s": {"median": round(float(np.median(shifts)), 2),
                        "iqr": [round(float(np.percentile(shifts, q)), 2) for q in (25, 75)],
                        "over_1s_pct": round(100 * float(np.mean(shifts > 1)), 1)},
        "hold_pause_s": {"median": round(float(np.median(holds)), 2),
                         "iqr": [round(float(np.percentile(holds, q)), 2) for q in (25, 75)]},
        # a timeout at T s of Level quiet: share of hold pauses it would cut into, share of shifts it would beat
        "timeout_curve": [{"T": T, "cuts_holds_pct": round(100 * float(np.mean(holds > T)), 1),
                           "earlier_than_engine_pct": round(100 * float(np.mean(shifts > T)), 1)}
                          for T in THRESHOLDS],
        "voice_vs_caller_dessa_timing": {"floor_takes": int(len(o)), "median_s": round(float(np.median(o)), 2),
                                         "overlap_start_pct": round(100 * float(np.mean(o < 0)), 1),
                                         "over_1s_pct": round(100 * float(np.mean(o > 1)), 1),
                                         "backchannels_per_min": round(bcs / minutes, 2)},
        "per_call": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_call"}, indent=1))
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
