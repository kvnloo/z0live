"""z0live voice bench: the frozen suite through mobile_live.Call on each voice engine.

Each case runs context_probe.py (real provider, synthetic speech via edge-tts, simulated phone over WebRTC,
delegation recorded as a fixture, no Hermes run) with HERMES_VOICE_ENGINE forced. A case passes when the
voice did not speak before the question, did not refuse, delegated exactly when the case says it must, and
its reply (or delegated request) matches. First audio = end of the asker's speech -> first sound back,
from the call trace (includes the provider's own end-of-turn wait). Usage:

  PATH=$PWD/.venv/bin:$PATH env -u PYTHONPATH .venv/bin/python bench.py [--suite bench/suite-v1.json]
      [--repeats 2] [--engines gpt-live,grok] > receipt.json

Prints one JSON line per case to stderr as it lands and the summary (pass rate, first-audio p50/p90,
errors, cases run) to stdout. Hand-check every failing row before acting on the number.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACES = Path(os.environ.get("TMPDIR", "/tmp")) / "z0live-bench-traces"


def first_audio(trace: Path):
    ends, first = [], None
    for line in trace.read_text().splitlines():
        e = json.loads(line)
        if e.get("ev") == "audio.off" and e.get("dir") == "input":
            ends.append(e.get("at") or e["t"])
        if e.get("ev") == "audio.on" and e.get("dir") == "output" and ends and first is None:
            first = e["t"]
    return round(first - ends[-1], 2) if first is not None and ends else None


def run(engine: str, case: dict) -> dict:
    before = set(TRACES.rglob("*.jsonl"))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(case.get("history", []), f)
    env = {**os.environ, "HERMES_VOICE_ENGINE": engine, "PROBE_ASK": case["ask"], "PROBE_HERMES_FIXTURE": "1",
           "Z0LIVE_CONTEXT_PROBE_TRACES": str(TRACES)}
    try:
        out = subprocess.run([sys.executable, str(HERE / "context_probe.py"), f.name], env=env,
                             capture_output=True, text=True, timeout=180, cwd=HERE)
        start = out.stdout.find("{")
        res = json.loads(out.stdout[start:]) if start >= 0 else {"error": out.stderr[-300:] or "no output"}
    except subprocess.TimeoutExpired:
        res = {"error": "timeout"}
    finally:
        os.unlink(f.name)
    new = sorted(set(TRACES.rglob("*.jsonl")) - before, key=lambda p: p.stat().st_mtime)
    reply, delegated = res.get("reply", ""), res.get("delegated") or []
    checks = {"no_catch_up": bool(res.get("pass_no_catch_up")), "no_refusal": bool(res.get("pass_no_refusal")),
              "delegation": bool(delegated) == case["delegate"]}
    if case.get("reply"):
        checks["reply"] = bool(re.search(case["reply"], reply, re.I))
    if case.get("request"):
        checks["request"] = any(re.search(case["request"], d, re.I) for d in delegated)
    return {"engine": engine, "case": case["id"], "kind": case["kind"], "pass": all(checks.values()) and "error" not in res,
            "checks": checks, "heard": res.get("heard"), "reply": reply, "delegated": delegated,
            "first_audio_s": first_audio(new[-1]) if new else None, "error": res.get("error")}


def summarize(rows):
    out = {}
    for engine in sorted({r["engine"] for r in rows}):
        mine = [r for r in rows if r["engine"] == engine]
        lat = sorted(r["first_audio_s"] for r in mine if r["first_audio_s"] is not None)
        q = statistics.quantiles(lat, n=10) if len(lat) >= 2 else [None] * 9
        out[engine] = {"pass": sum(r["pass"] for r in mine), "of": len(mine),
                       "pass_rate": round(sum(r["pass"] for r in mine) / len(mine), 3),
                       "first_audio_p50_s": statistics.median(lat) if lat else None, "first_audio_p90_s": q[8],
                       "errors": sum(bool(r["error"]) for r in mine),
                       "by_kind": {k: f"{sum(r['pass'] for r in mine if r['kind'] == k)}/{sum(r['kind'] == k for r in mine)}"
                                   for k in sorted({r['kind'] for r in mine})}}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default=str(HERE / "bench" / "suite-v1.json"))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--engines", default="gpt-live,grok")
    o = ap.parse_args()
    suite = json.loads(Path(o.suite).read_text())
    engines = o.engines.split(",")
    rows = []
    for rep in range(o.repeats):
        for case in suite["cases"]:
            for engine in engines:  # interleaved so a provider hiccup hits both
                rows.append({**run(engine, case), "repeat": rep})
                print(json.dumps(rows[-1]), file=sys.stderr, flush=True)
                time.sleep(2)
    print(json.dumps({"suite": suite["suite"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "summary": summarize(rows), "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
