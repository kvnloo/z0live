#!/usr/bin/env python3
"""Cross-check: does bench/levels_replay.py find the barge-in onsets the bargein_replay sim knows are true?

Harness cross-check, not a keep/revert of product code. Offline, CPU only, no audio device, service or network.

Pipeline per trial (fixed seeds, deterministic):
  1. bench/bargein_replay.py (pinned copy of exp/bargein-ae-rho-veto @ 132b2b4, bargein_replay/25, unmodified)
     generates the trial with the realistic opt-in acoustics: --echo-model residual --echo-continuous
     --bot-render recorded (seeded room echo of a LibriSpeech bot render in every uplink sample while the speaker
     plays, near-end noise, caller clip at the labelled onset). The sim's own `replay` runs with arm "none": a
     name the sim has no flush rule for (every flush path is guarded by an arm equality), so the adapter plays the
     whole render and the downlink the barge-in is defined against is never cut early. Arm A (flush on the
     provider speech_started event) is also run and reported as a secondary breakdown, not the verdict.
     Amendment (before any scored run, after one plumbing smoke trial): with continuous echo, arm A false-cuts
     on the bot's own echo (smoke seed 39 trial 0: flush at 314 ms, labelled onset 764 ms), so its downlink is
     gone before the caller speaks and the trial cannot test the scorer; the primary arm moved from A to none.
  2. Per-direction PCM, taken from the sim itself, never re-synthesised:
       uplink   ("input")  every 10 ms packet the sim's `capture` returns (recorded by wrapping that function),
                           reaching the adapter at capture end + the trial's uplink leg (`up`).
       downlink ("output") one 20 ms frame per adapter play tick (audio_transit + (i + depth) x 20 ms): bot frame i
                           while the adapter still played it (tick <= trace last_adapter_frame_ms), digital silence
                           after a flush (a WebRTC output track keeps ticking; arm A only).
  3. Those int16 frames go, in adapter-clock order, through the real mobile_live.Level.frame with
     Z0LIVE_TRACE_LEVELS=1 into a real call_trace.Trace JSONL (time.monotonic patched to the virtual clock), so the
     audio.levels events (250 ms windows, per-frame RMS/peak dBFS, n samples) are byte-for-byte what the adapter's
     emitter would write. Frames are 16 kHz mono, so the scorer runs with --rate 16000 (other params default).
  4. levels_replay.parse + score on that JSONL.

Truth: the sim's labelled caller mic onset (trial onset, ms; sim t=0 = first bot frame generated = trace t0).
Onset error = scorer onset t_ms - truth (signed; includes the uplink leg and 10 ms packetisation, ~+8..+30 ms).

PREREGISTRATION (fixed before the first run):
  seeds 39, 40 x 30 trials each (60 trials: 42 valid barge-ins, 18 negatives: 6 backchannel, 6 echo, 6 noise).
  match     a valid trial is detected when a scorer onset lies in [truth - 100, truth + 400] ms; the first such
            onset is the match. Every other onset (any trial, any kind) is a false onset.
  primary   detection rate over valid trials and median |onset error| ms over detected trials.
  verdict   AGREE if detection >= 0.90 and median |err| <= 60 ms (<= 3 frames of 20 ms) and false onsets
            <= 0.1 per trial (over all 60 trials); otherwise DISAGREE with the breakdown (by kind, signed error,
            p95). NULL if the sim cannot produce per-direction PCM.

Output: JSON, schema levels_xcheck/1 (sorted keys, deterministic).

  python3 bench/levels_xcheck.py --output out.json
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
SCHEMA = "levels_xcheck/1"
SIM_PIN = "exp/bargein-ae-rho-veto@132b2b4 (bargein_replay/25)"
SIM_FLAGS = ("--echo-model", "residual", "--echo-continuous", "--bot-render", "recorded")
SEEDS = (39, 40)
TRIALS = 30
ARM = "none"         # primary: no flush rule in the sim, the whole render plays
ARMS = (ARM, "A")    # A: secondary breakdown only
MATCH_LO_MS, MATCH_HI_MS = -100, 400
RATE = 16000


def _imports():
    for p in (str(HERE), str(HERE.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("HERMES_MOBILE_VOICE_LIAISON", "0")
    os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
    import bargein_replay as br
    import call_trace
    import levels_replay as lr
    import mobile_live as live
    return br, call_trace, lr, live


br, call_trace, lr, live = _imports()
import av  # noqa: E402  (after mobile_live: same PyAV the adapter decodes with)


class _Cfg(Exception):
    pass


def sim_cfg(seed, trials, flags=SIM_FLAGS):
    """The sim's own argparse namespace + validation (its main(), stopped right before run())."""
    def stop(n, s, cfg, bot):
        raise _Cfg(cfg)
    argv = ["bargein_replay.py", "--seed", str(seed), "--trials", str(max(trials, 10)), *flags]
    with mock.patch.object(sys, "argv", argv), mock.patch.object(br, "run", stop):
        try:
            br.main()
        except _Cfg as got:
            return got.args[0]
    raise RuntimeError("bargein_replay.main did not reach run()")


def sim_trial(trial, cfg, arm=ARM):
    """Run the sim's replay; return (row, uplink [(adapter_ms, int16 packet)], downlink [(adapter_ms, frame)])."""
    packets = []
    real = br.capture

    def recording(tr, speaker, start, size, c):
        packet = real(tr, speaker, start, size, c)
        packets.append(((start + size) // br.SAMPLES_MS, np.frombuffer(packet.tobytes(), dtype=np.int16).copy()))
        return packet

    bot_default = br.voice(br.BOT_MS, 142, 11000, 115) if "bot" not in trial else None
    with mock.patch.object(br, "capture", recording):
        row = br.replay(trial, arm, cfg, bot_default)
    if not packets:
        raise RuntimeError("sim produced no uplink PCM")
    up = [(end + trial["up"], p) for end, p in packets]
    bot = trial.get("bot", bot_default)
    bot = np.frombuffer(bot.tobytes(), dtype=np.int16)
    last = row["trace"].get("last_adapter_frame_ms")
    size = br.FRAME_MS * br.SAMPLES_MS
    down, played = [], 0
    for i in range(br.BOT_MS // br.FRAME_MS):
        tick = cfg.audio_transit_ms + (i + cfg.depth) * br.FRAME_MS
        if last is not None and tick <= last:
            frame, played = bot[i * size:(i + 1) * size].copy(), played + 1
        else:
            frame = np.zeros(size, dtype=np.int16)
        down.append((tick, frame))
    if "adapter_flush_ms" in row["trace"]:  # consistency with the sim's own cursor
        assert played * br.FRAME_MS == row["trace"]["truncate_audio_end_ms"], (played, row["trace"])
    return row, up, down


def _frame(pcm):
    f = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    f.sample_rate = RATE
    return f


class _Call:
    def __init__(self, trace):
        self.trace, self.muted = trace, False


@contextmanager
def _virtual_clock():
    clock = {"now": 1000.0}
    with mock.patch("time.monotonic", lambda: clock["now"]), mock.patch("time.time", lambda: 1.8e9 + clock["now"]):
        yield clock


def levels_jsonl(up, down, directory):
    """Feed both directions through mobile_live.Level into a real call_trace.Trace; return the JSONL lines."""
    events = sorted([(t, 0, "input", p) for t, p in up] + [(t, 1, "output", p) for t, p in down],
                    key=lambda e: (e[0], e[1]))
    env = {"Z0LIVE_TRACE_LEVELS": "1", "HERMES_MOBILE_VOICE_TRACE": "1"}
    with mock.patch.dict(os.environ, env), _virtual_clock() as clock:
        t0 = clock["now"]
        trace = call_trace.Trace("xcheck", Path(directory))
        call = _Call(trace)
        levels = {"input": live.Level(call, "input"), "output": live.Level(call, "output")}
        for t_ms, _, direction, pcm in events:
            clock["now"] = t0 + t_ms / 1000.0
            levels[direction].frame(_frame(pcm))
        if trace.f:
            trace.f.close()
        path = trace.path
    return path.read_text(encoding="utf-8").splitlines()


def match(onsets, truth, kind):
    """(matched onset or None, false onsets) under the preregistered window."""
    hit = None
    if kind == "valid":
        hit = next((o for o in onsets if MATCH_LO_MS <= o["t_ms"] - truth <= MATCH_HI_MS), None)
    return hit, [o for o in onsets if o is not hit]


def _pct(values, q):
    return round(float(np.percentile(values, q)), 1) if values else None


def check_trial(trial, cfg, directory, params, arm=ARM):
    row, up, down = sim_trial(trial, cfg, arm)
    lines = levels_jsonl(up, down, directory)
    rep = lr.score(lr.parse(lines), params)
    onsets = rep["bargein"]["onsets"]
    hit, false = match(onsets, trial["onset"], trial["kind"])
    truth = trial["onset"]
    return {
        "id": trial["id"], "kind": trial["kind"], "truth_ms": truth, "up_ms": trial["up"],
        "down_active_at_truth": any(s["start_ms"] <= truth < s["end_ms"] + params.hangover_ms
                                    for s in rep["downlink"]["spans"]),
        "flush_ms": row["trace"].get("adapter_flush_ms"), "levels_events": sum('"audio.levels"' in x for x in lines),
        "echo_floor_db": rep["echo"]["floor_db"], "threshold_db": rep["bargein"]["threshold_db"],
        "downlink_spans": len(rep["downlink"]["spans"]), "onsets_ms": [o["t_ms"] for o in onsets],
        "detected": hit is not None if trial["kind"] == "valid" else None,
        "err_ms": hit["t_ms"] - trial["onset"] if hit else None, "false_onsets": len(false),
        "integrity": {d: {k: v for k, v in g.items() if k in ("frames", "gaps", "duplicate_t", "non_monotonic_t")}
                      for d, g in rep["integrity"]["dirs"].items()},
    }


def summarise(rows):
    valid = [r for r in rows if r["kind"] == "valid"]
    errs = [r["err_ms"] for r in valid if r["detected"]]
    abs_errs = [abs(e) for e in errs]
    n_false = sum(r["false_onsets"] for r in rows)
    det = len(errs) / len(valid) if valid else 0.0
    med = _pct(abs_errs, 50)
    fpt = n_false / len(rows) if rows else 0.0
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"n": 0, "false_onsets": 0, "trials_with_false": 0})
        k["n"] += 1
        k["false_onsets"] += r["false_onsets"]
        k["trials_with_false"] += r["false_onsets"] > 0
    checks = {"detection_ge_0.90": det >= 0.90, "median_abs_err_le_60ms": med is not None and med <= 60,
              "false_per_trial_le_0.1": fpt <= 0.1}
    return {
        "n_trials": len(rows), "n_valid": len(valid), "detected": len(errs), "detection_rate": round(det, 4),
        "abs_err_ms": {"median": med, "p95": _pct(abs_errs, 95), "max": max(abs_errs) if abs_errs else None},
        "signed_err_ms": {"median": _pct(errs, 50), "p5": _pct(errs, 5), "p95": _pct(errs, 95)},
        "false_onsets": n_false, "false_onsets_per_trial": round(fpt, 4), "by_kind": by_kind,
        "missed": [[r["seed"], r["id"]] for r in valid if not r["detected"]],
        "valid_down_active_at_truth": sum(r["down_active_at_truth"] for r in valid),
        "checks": checks, "verdict": "AGREE" if all(checks.values()) else "DISAGREE",
    }


def run(seeds=SEEDS, trials=TRIALS, arms=ARMS, floor_mode=None):
    """Primary verdict from arms[0]; the other arms are reported as breakdowns only. floor_mode None = scorer
    default."""
    params = lr.Params(rate=RATE) if floor_mode is None else lr.Params(rate=RATE, floor_mode=floor_mode)
    out = {"schema": SCHEMA, "sim": {"pin": SIM_PIN, "flags": list(SIM_FLAGS), "primary_arm": arms[0],
                                     "arms": list(arms), "seeds": list(seeds), "trials_per_seed": trials},
           "scorer": {"params": asdict(params)}, "match_window_ms": [MATCH_LO_MS, MATCH_HI_MS], "arms": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for arm in arms:
            rows, per_seed = [], {}
            for seed in seeds:
                cfg = sim_cfg(seed, trials)
                seed_rows = []
                for trial in br.stimuli(trials, seed, cfg.echo_model, cfg.bot_render, cfg.caller_level_db_range,
                                        cfg.erl_db_range, cfg.clip_set):
                    where = Path(tmp) / f"{arm}-{seed}-{trial['id']}"
                    seed_rows.append({"seed": seed, **check_trial(trial, cfg, where, params, arm)})
                per_seed[str(seed)] = summarise(seed_rows)
                rows += seed_rows
            out["arms"][arm] = {"per_seed": per_seed, "summary": summarise(rows), "trials": rows}
    out["summary"] = out["arms"][arms[0]]["summary"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=str, default=",".join(map(str, SEEDS)), help="comma list (prereg: 39,40)")
    ap.add_argument("--trials", type=int, default=TRIALS, help="trials per seed (prereg: 30)")
    ap.add_argument("--arms", type=str, default=",".join(ARMS), help="comma list, first is primary (prereg: none,A)")
    ap.add_argument("--floor-mode", choices=lr.FLOOR_MODES, help="levels_replay echo-floor estimator (default: its)")
    ap.add_argument("--output", type=Path, help="write the full JSON here")
    a = ap.parse_args(argv)
    res = run(tuple(int(s) for s in a.seeds.split(",")), a.trials, tuple(a.arms.split(",")), a.floor_mode)
    text = json.dumps(res, sort_keys=True, indent=1) + "\n"
    if a.output:
        a.output.write_text(text, encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "summary": res["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
