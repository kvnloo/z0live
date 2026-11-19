"""Engine A/B: the same spoken reasoning questions through mobile_live.Call on each voice engine.

Each question runs context_probe.py (real provider, synthetic speech via edge-tts, simulated phone over
WebRTC) with HERMES_VOICE_ENGINE forced, then scores the reply against a regex and reads the call trace
for end-of-speech -> first audio back. Usage:
  PATH=$PWD/.venv/bin:$PATH env -u PYTHONPATH .venv/bin/python engine_ab.py [gpt-live grok] > receipt.json
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

QUESTIONS = [  # (spoken ask, answer regex) - short spoken reasoning, no tools needed
    ("I have three apples and eat one, then buy twice as many as I have left. How many apples do I have now?", r"\b(six|6)\b"),
    ("What is seventeen times twenty-three?", r"\b(391|three hundred (and )?ninety[- ]one)\b"),
    ("Alice is taller than Bob, and Bob is taller than Carol. Who is the shortest?", r"\bcarol\b"),
    ("A train leaves at nine forty and the trip takes one hour and thirty-five minutes. What time does it arrive?", r"\b(11:15|eleven fifteen|quarter past eleven)\b"),
    ("Tom always lies. Tom says it is raining. Is it raining?", r"\b(no|not raining|isn.t raining|it isn.t)\b"),  # 11-19: "...so it isn't" was right but unmatched
    ("How many letter R's are in the word strawberry?", r"\b(three|3)\b"),
]
HERE = Path(__file__).resolve().parent
TRACES = Path(os.environ.get("TMPDIR", "/tmp")) / "engine-ab-traces"


def latency(trace: Path):
    """Seconds from the end of the asker's speech to the first audio back (None if unseen)."""
    ends, first = [], None
    for line in trace.read_text().splitlines():
        e = json.loads(line)
        if e.get("ev") == "audio.off" and e.get("dir") == "input":
            ends.append(e.get("at") or e["t"])
        if e.get("ev") == "audio.on" and e.get("dir") == "output" and ends and first is None:
            first = e["t"]
    return round(first - ends[-1], 2) if first is not None and ends else None


def run(engine, ask):
    before = set(TRACES.rglob("*.jsonl"))
    env = {**os.environ, "HERMES_VOICE_ENGINE": engine, "PROBE_ASK": ask, "HERMES_MOBILE_VOICE_TRACE_DIR": str(TRACES)}
    out = subprocess.run([sys.executable, str(HERE / "context_probe.py"), "/dev/stdin"], input="[]", env=env,
                         capture_output=True, text=True, timeout=180, cwd=HERE)
    start = out.stdout.find("{")
    result = json.loads(out.stdout[start:]) if start >= 0 else {"error": out.stderr[-300:]}
    new = sorted(set(TRACES.rglob("*.jsonl")) - before, key=lambda p: p.stat().st_mtime)
    return result, (latency(new[-1]) if new else None)


def main():
    engines = sys.argv[1:] or ["gpt-live", "grok"]
    rows = []
    for ask, pattern in QUESTIONS:
        for engine in engines:  # interleaved so a provider hiccup hits both, not one
            result, lat = run(engine, ask)
            reply = result.get("reply", "")
            rows.append({"engine": engine, "ask": ask, "heard": result.get("heard"), "reply": reply,
                         "correct": bool(re.search(pattern, reply, re.I)), "first_audio_s": lat,
                         "error": result.get("error")})
            print(json.dumps(rows[-1]), file=sys.stderr, flush=True)
            time.sleep(2)
    summary = {}
    for engine in engines:
        mine = [r for r in rows if r["engine"] == engine]
        lats = sorted(r["first_audio_s"] for r in mine if r["first_audio_s"] is not None)
        summary[engine] = {"correct": sum(r["correct"] for r in mine), "of": len(mine),
                           "first_audio_median_s": lats[len(lats) // 2] if lats else None}
    print(json.dumps({"summary": summary, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
