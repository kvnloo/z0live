"""SLM lane bench: speed and spoken-answer quality of small models in the tiers.py lane prompt.

Every case is sent exactly as a lane sends it: tiers.SYSTEM + tiers.snapshot(brief, call turns) as the system
message, the ask as the user message. Local GGUFs run one at a time in llama-server (Metal, all layers on GPU,
greedy, max 120 tokens, thinking off where the template allows); hosted models go through lane_server.py.

Per case: correct (answer regex, or SKIP when the lane must leave it to Hermes), spoken (no markdown, <= 45
words, <= 3 sentences), usable = correct and spoken (skip cases: correct). Speed: first visible token (hidden
thinking counts against it), decode tokens/s, total. Optional --judge: Opus rates each non-skip reply 1-5 for
how good it is to hear on a call (one batched call per model).

  .venv/bin/python slm_bench.py --models qwen3-1.7b,lfm3-1.7b [--hosted claude-haiku-5-5] [--judge] > receipt.json
"""
import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import tiers

HERE = Path(__file__).resolve().parent
MODELS = Path(os.environ.get("Z0LIVE_MODELS", str(Path.home() / ".local/share/z0live/models")))
SERVER = os.environ.get("LLAMA_SERVER", str(Path.home() / ".local/opt/llama.cpp/b12527/llama-b12527/llama-server"))
LANE = os.environ.get("Z0LIVE_LANE_URL", "http://127.0.0.1:8646/v1/ask")
CLAUDE = "claude-subscription-directsdk-experimental"
PORT = 18080
MARKDOWN = re.compile(r"(\*\*|`|^\s*[-*#]\s|^\s*\d+\.\s)", re.M)
THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def messages(suite, case):
    system = tiers.SYSTEM.format(tier="fast") + "\n\n" + tiers.snapshot(suite["brief"], case.get("turns", []))
    return system, case["ask"]


def score(case, reply):
    text = THINK.sub("", reply).strip()
    is_skip = bool(tiers.SKIP.match(text))
    words, sentences = len(text.split()), len([s for s in re.split(r"[.!?]+(\s|$)", text) if s and s.strip()])
    spoken = bool(text) and not MARKDOWN.search(text) and words <= 45 and sentences <= 3
    if case.get("skip"):
        correct = is_skip
        return {"correct": correct, "spoken": True, "usable": correct, "words": words, "reply": text}
    correct = not is_skip and bool(re.search(case["answer"], text, re.I))
    return {"correct": correct, "spoken": spoken, "usable": correct and spoken, "words": words, "reply": text}


def post(url, body, timeout=120):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def ask_local(system, user, cache=True):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True, "temperature": 0, "max_tokens": 120, "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": cache}
    t0, first, text, timings = time.monotonic(), None, "", {}
    with post(f"http://127.0.0.1:{PORT}/v1/chat/completions", body) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            chunk = json.loads(line[5:])
            timings = chunk.get("timings") or timings
            piece = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if piece and first is None and THINK.sub("", text + piece).strip():
                first = time.monotonic() - t0
            text += piece
    return text, first, time.monotonic() - t0, timings.get("predicted_per_second")


def ask_hosted(provider, model, system, user):
    with post(LANE, {"provider": provider, "model": model, "system": system, "user": user, "max_tokens": 120}) as r:
        out = json.load(r)
    return out["text"], out["first_s"], out["total_s"], None


def serve(path):
    proc = subprocess.Popen([SERVER, "-m", str(path), "--port", str(PORT), "-c", "8192", "-ngl", "99", "-np", "1",
                             "--jinja", "--no-webui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(240):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if json.load(r).get("status") == "ok":
                    return proc
        except Exception:  # noqa: BLE001 - still loading
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited {proc.returncode} for {path.name}")
        time.sleep(0.5)
    raise RuntimeError(f"llama-server never became healthy for {path.name}")


def stop(proc):
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


def run_model(name, suite, ask, cold=None):
    """first_s per case is with the brief already in the prompt cache (mid-call: the brief does not change);
    cold_first_s is one request with the cache off (the first lane request after a new brief)."""
    ask(*messages(suite, suite["cases"][0]))  # warm: weights paged in, graph built, prompt cache primed
    cold_first = None
    if cold:
        _, cold_first, *_ = cold(*messages(suite, suite["cases"][0]))
    rows = []
    for case in suite["cases"]:
        try:
            reply, first, total, tps = ask(*messages(suite, case))
            row = {**score(case, reply), "first_s": round(first, 3) if first else None, "total_s": round(total, 3),
                   "tok_s": round(tps, 1) if tps else None}
        except Exception as exc:  # noqa: BLE001 - a failed case is a wrong answer, never a crash
            row = {"correct": False, "spoken": False, "usable": False, "reply": "", "error": str(exc)[:200]}
        rows.append({"model": name, "case": case["id"], "kind": case["kind"], **row})
        print(json.dumps(rows[-1]), file=sys.stderr, flush=True)
    if rows:
        rows[0]["cold_first_s"] = round(cold_first, 3) if cold_first else None
    return rows


def judge(suite, rows):
    """Opus rates each non-skip reply 1-5 for a live call (5 = exactly what you'd want to hear). {case: score}"""
    by_id = {c["id"]: c for c in suite["cases"]}
    items = [{"id": r["case"], "question": by_id[r["case"]]["ask"], "reply": r["reply"]}
             for r in rows if not by_id[r["case"]].get("skip")]
    prompt = ("Rate each reply a voice assistant gave on a live phone call, 1-5: 5 = correct, natural to hear, "
              "concise, most important fact first; 3 = right but clumsy or padded; 1 = wrong, empty, or unusable. "
              "Context the assistant had: " + suite["brief"] + "\nReturn only a JSON object {id: score}.\n\n"
              + json.dumps(items, indent=0))
    try:
        text, *_ = ask_hosted(CLAUDE, "claude-opus-5-5", "", prompt)
        return {k: int(v) for k, v in json.loads(text[text.find("{"): text.rfind("}") + 1]).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"judge failed: {exc}", file=sys.stderr)
        return {}


def summarize(name, rows, scores, size):
    first = sorted(r["first_s"] for r in rows if r.get("first_s"))
    q = statistics.quantiles(first, n=10) if len(first) >= 2 else [None] * 9
    kinds = sorted({r["kind"] for r in rows})
    return {"model": name, "size_gb": round(size / 1e9, 2) if size else None,
            "usable": round(sum(r["usable"] for r in rows) / len(rows), 3),
            "correct": round(sum(r["correct"] for r in rows) / len(rows), 3),
            "by_kind": {k: f"{sum(r['usable'] for r in rows if r['kind'] == k)}/{sum(r['kind'] == k for r in rows)}"
                        for k in kinds},
            "first_p50_s": statistics.median(first) if first else None, "first_p90_s": q[8],
            "cold_first_s": rows[0].get("cold_first_s") if rows else None,
            "total_p50_s": statistics.median(r["total_s"] for r in rows if r.get("total_s")) if rows else None,
            "tok_s_p50": statistics.median([r["tok_s"] for r in rows if r.get("tok_s")] or [0]) or None,
            "judge_mean": round(statistics.mean(scores.values()), 2) if scores else None,
            "errors": sum(bool(r.get("error")) for r in rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default=str(HERE / "bench" / "slm-v1.json"))
    ap.add_argument("--models", default="", help="local GGUF names under $Z0LIVE_MODELS (without .gguf)")
    ap.add_argument("--hosted", default="", help="lane_server models, provider:model or a bare Claude model")
    ap.add_argument("--judge", action="store_true")
    o = ap.parse_args()
    suite = json.loads(Path(o.suite).read_text())
    summaries, all_rows = [], []
    for name in filter(None, o.models.split(",")):
        path = MODELS / f"{name}.gguf"
        proc = serve(path)
        try:
            rows = run_model(name, suite, ask_local, cold=lambda s, u: ask_local(s, u, cache=False))
        finally:
            stop(proc)
        scores = judge(suite, rows) if o.judge else {}
        summaries.append(summarize(name, rows, scores, path.stat().st_size))
        all_rows += rows
        print(json.dumps(summaries[-1]), file=sys.stderr, flush=True)
    for spec in filter(None, o.hosted.split(",")):
        provider, model = spec.split(":", 1) if ":" in spec else (CLAUDE, spec)
        rows = run_model(model, suite, lambda s, u, p=provider, m=model: ask_hosted(p, m, s, u))
        scores = judge(suite, rows) if o.judge else {}
        summaries.append(summarize(model, rows, scores, None))
        all_rows += rows
        print(json.dumps(summaries[-1]), file=sys.stderr, flush=True)
    print(json.dumps({"suite": suite["suite"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "server": SERVER,
                      "summary": summaries, "rows": all_rows}, indent=1))


if __name__ == "__main__":
    main()
