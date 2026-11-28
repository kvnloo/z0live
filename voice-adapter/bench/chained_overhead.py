#!/usr/bin/env python3
"""Adapter-added overhead of chained_engine.ChainedEngine (on_device STT -> model -> TTS) with fake stages.

Fake stages sleep a fixed injected latency (stt before returning, model before its first token, tts before
the first chunk of the first clause only), then return instantly. Per turn:
  overhead_injected = EOS->first_audio - (stt + model_first + tts_first injected)   (includes asyncio timer overshoot)
  overhead_stage    = EOS->first_audio - (same three gaps measured inside the fakes) (engine-only cost)
Arms: zero (0/0/0 ms) run twice for an A/A noise band, lat50 (50/50/50 ms) once. Each arm: RUNS fresh event
loops x TURNS scored turns (WARMUP discarded). Prints one JSON object (or writes it to --out).
"""
import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chained_engine as ce  # noqa: E402

PCM = b"\x00\x00" * 320  # 20 ms of 16 kHz mono silence; fakes ignore content
REPLY = ("Sure, ", "it is ", "noon. ", "Anything ", "else?")
CHUNK = b"\x01\x00" * 480
CHUNKS_PER_CLAUSE = 3


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fakes(stt_ms, model_ms, tts_ms, gaps):
    async def stt(pcm):
        t = time.perf_counter_ns()
        if stt_ms:
            await asyncio.sleep(stt_ms / 1000)
        gaps["stt"] = time.perf_counter_ns() - t
        return "what time is it"

    async def model(text):
        t = time.perf_counter_ns()
        if model_ms:
            await asyncio.sleep(model_ms / 1000)
        gaps["model"] = time.perf_counter_ns() - t
        for tok in REPLY:
            yield tok

    async def tts(clause):
        # Only the first clause sleeps: EOS->first_audio ends at that clause's first chunk, so later clauses' latency
        # never entered the metric but cost ~tts_ms of wall per turn (the 319 s > 300 s budget miss in OD-harness).
        t = time.perf_counter_ns()
        if tts_ms and "tts" not in gaps:
            await asyncio.sleep(tts_ms / 1000)
        gaps.setdefault("tts", time.perf_counter_ns() - t)  # first clause's first chunk only
        for _ in range(CHUNKS_PER_CLAUSE):
            yield CHUNK

    return stt, model, tts


async def one_run(cfg, turns, warmup):
    injected_ns = int((cfg["stt"] + cfg["model"] + cfg["tts"]) * 1e6)
    inj, stg, digest = [], [], hashlib.sha256()
    errors = work = 0
    for i in range(warmup + turns):
        gaps, audio = {}, []
        engine = ce.ChainedEngine(*fakes(cfg["stt"], cfg["model"], cfg["tts"], gaps), on_audio=audio.append)
        r = await engine.turn(PCM, turn_id=i)
        if i < warmup:
            continue
        if not r.ok or r.first_audio_ns is None:
            errors += 1
            continue
        work += 1
        digest.update(f"{r.text}|{r.reply}|{r.chunks}|{r.bytes}".encode())
        inj.append((r.first_audio_ns - injected_ns) / 1000)
        stg.append((r.first_audio_ns - gaps["stt"] - gaps["model"] - gaps["tts"]) / 1000)
    return {"error_count": errors, "work_count": work, "output_sha256": digest.hexdigest(),
            "overhead_injected_us": {"p50": pct(inj, .5), "p95": pct(inj, .95)},
            "overhead_stage_us": {"p50": pct(stg, .5), "p95": pct(stg, .95)},
            "raw_injected_us": inj, "raw_stage_us": stg}


def arm(name, cfg, runs, turns, warmup):
    t = time.monotonic()
    rs = [asyncio.run(one_run(cfg, turns, warmup)) for _ in range(runs)]
    summary = {"arm": name, "config_ms": cfg, "runs": runs, "turns": turns, "warmup": warmup,
               "error_count": sum(r["error_count"] for r in rs), "work_count": sum(r["work_count"] for r in rs),
               "deterministic": len({r["output_sha256"] for r in rs}) == 1,
               "output_sha256": rs[0]["output_sha256"], "wall_s": round(time.monotonic() - t, 2)}
    for key in ("overhead_injected_us", "overhead_stage_us"):
        p50s = [r[key]["p50"] for r in rs]
        p95s = [r[key]["p95"] for r in rs]
        pooled = [x for r in rs for x in r["raw_" + key[9:]]]
        summary[key] = {"p50_median_of_runs": statistics.median(p50s), "p95_median_of_runs": statistics.median(p95s),
                        "p50_run_min": min(p50s), "p50_run_max": max(p50s),
                        "p50_pooled": pct(pooled, .5), "p95_pooled": pct(pooled, .95)}
    summary["per_run"] = [{k: v for k, v in r.items() if not k.startswith("raw_")} for r in rs]
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--turns", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--out")
    a = ap.parse_args()
    zero, lat50 = {"stt": 0, "model": 0, "tts": 0}, {"stt": 50, "model": 50, "tts": 50}
    t = time.monotonic()
    arms = [arm("zero_A", zero, a.runs, a.turns, a.warmup), arm("zero_A2", zero, a.runs, a.turns, a.warmup),
            arm("lat50", lat50, a.runs, a.turns, a.warmup)]
    za, zb = arms[0]["overhead_injected_us"], arms[1]["overhead_injected_us"]
    out = {"bench": "chained_overhead", "python": sys.version.split()[0], "machine": platform.machine(),
           "arms": arms, "aa_band_us": {"p50": abs(za["p50_median_of_runs"] - zb["p50_median_of_runs"]),
                                         "p95": abs(za["p95_median_of_runs"] - zb["p95_median_of_runs"])},
           "wall_s": round(time.monotonic() - t, 2)}
    text = json.dumps(out, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({k: v for k, v in out.items() if k != "arms"} |
                     {"arms": [{k: v for k, v in x.items() if k != "per_run"} for x in arms]}, indent=1))


if __name__ == "__main__":
    main()
