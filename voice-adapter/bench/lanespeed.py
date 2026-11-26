"""C56 lane-speed climb: first-answer latency of one local GGUF on the frozen slm-v2 suite under ABAB server blocks.

  LLAMA_SERVER=.../llama-server python lanespeed.py --gguf m.gguf --blocks A:512,B:2048,A:512,B:2048 --out r.json

Each block starts a fresh llama-server with one knob (`-ub N`, physical batch size) and otherwise identical flags,
sends 1 unscored warm-up and then every slm-v2 case once, in suite order, exactly as slm_bench sends a lane request
(tiers.SYSTEM + tiers.snapshot(brief, turns), greedy, max 120 tokens, thinking off) but with cache_prompt false so
every request re-prefills the full brief (prefix reuse is a different knob, tested in S25-B3q/B3r).
Scoring is slm_bench.score unchanged. Timing per case:
  ttft_s           request -> first visible content token
  first_sentence_s request -> streamed visible text first holds a sentence end ([.!?] then whitespace) or the stream
                   ends (a reply with no terminator counts at end of stream)
  total_s, prompt_n, prompt_ms, predicted_n, predicted_ms (server timings)
Synthetic suite only, no owner data.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import slm_bench  # noqa: E402

PORT = 18091
SENTENCE_END = re.compile(r"[.!?](\s)")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def serve(server, gguf, ub, log):
    cmd = [server, "-m", str(gguf), "--host", "127.0.0.1", "--port", str(PORT), "-c", "8192", "-np", "1",
           "--cache-ram", "0", "--jinja", "--no-webui", "-ub", str(ub)]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(600):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if json.load(r).get("status") == "ok":
                    return proc, cmd
        except Exception:  # noqa: BLE001 - still loading
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited {proc.returncode}")
        time.sleep(0.5)
    raise RuntimeError("llama-server never became healthy")


def stop(proc):
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


def ask(system, user):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True, "temperature": 0, "seed": 42, "max_tokens": 120,
            "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0, first, sentence, text, timings = time.monotonic(), None, None, "", {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            chunk = json.loads(line[5:])
            timings = chunk.get("timings") or timings
            piece = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if not piece:
                continue
            text += piece
            visible = slm_bench.THINK.sub("", text).lstrip()
            now = time.monotonic() - t0
            if first is None and visible.strip():
                first = now
            if sentence is None and SENTENCE_END.search(visible):
                sentence = now
    total = time.monotonic() - t0
    return text, first, sentence if sentence is not None else total, total, timings


def p50(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--blocks", required=True, help="ARM:UB,... e.g. A:512,B:2048,A:512,B:2048")
    ap.add_argument("--suite", default=str(HERE / "slm-v2.json"))
    ap.add_argument("--suite-b", default=None, help="C56-brief: suite for arm B blocks (same cases, other brief); "
                    "default = --suite")
    ap.add_argument("--deadline-s", type=float, default=2100, help="stop starting new cases after this wall time")
    ap.add_argument("--every", type=int, default=1, help="score suite cases with index %% EVERY == 0 (v2: 2)")
    ap.add_argument("--meta", default="{}")
    ap.add_argument("--out", required=True)
    ap.add_argument("--logdir", default=".")
    o = ap.parse_args()
    server = os.environ["LLAMA_SERVER"]
    suite_path, gguf = Path(o.suite), Path(o.gguf)
    suite = json.loads(suite_path.read_text())
    suite_b_path = Path(o.suite_b) if o.suite_b else suite_path
    suites = {"A": (suite_path, suite), "B": (suite_b_path, json.loads(suite_b_path.read_text()))}
    assert suites["B"][1]["cases"] == suite["cases"], "arm suites must share identical cases"
    cases = [c for i, c in enumerate(suite["cases"]) if i % o.every == 0]
    cpu = None
    if Path("/proc/cpuinfo").exists():
        cpu = next((ln.split(":", 1)[1].strip() for ln in Path("/proc/cpuinfo").read_text().splitlines()
                    if ln.startswith("model name")), None)
    out = {"schema": "z0live.lanespeed.v1", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           **json.loads(o.meta), "gguf_sha256": sha256(gguf), "suite_sha256": sha256(suite_path),
           "arm_suite": {a: {"path": p.name, "sha256": sha256(p), "brief_chars": len(s["brief"])}
                         for a, (p, s) in suites.items()},
           "harness_sha256": {f: sha256(p)[:16] for f, p in (("lanespeed.py", Path(__file__)),
                                                              ("slm_bench.py", HERE.parent / "slm_bench.py"),
                                                              ("tiers.py", HERE.parent / "tiers.py"))},
           "runner": {"cpus": os.cpu_count(), "machine": platform.machine(), "cpu": cpu},
           "complete": False, "blocks": []}
    t_start = time.monotonic()
    stopped = False
    for bi, spec in enumerate(filter(None, o.blocks.split(","))):
        arm, ub = spec.split(":")
        bsuite = suites.get(arm, (suite_path, suite))[1]
        label = f"{arm}{sum(1 for b in out['blocks'] if b['arm'] == arm) + 1}"
        with open(Path(o.logdir) / f"server-{bi}-{label}.log", "w") as log:
            t0 = time.monotonic()
            proc, cmd = serve(server, gguf, int(ub), log)
            block = {"block": bi, "label": label, "arm": arm, "ub": int(ub), "suite": bsuite["suite"], "cmd": [os.path.basename(cmd[0])] + cmd[1:],
                     "load_s": round(time.monotonic() - t0, 2), "rows": []}
            try:
                ask(*slm_bench.messages(bsuite, bsuite["cases"][0]))  # unscored warm-up
                for case in cases:
                    if time.monotonic() - t_start > o.deadline_s:
                        stopped = True
                        break
                    try:
                        reply, first, sent, total, tm = ask(*slm_bench.messages(bsuite, case))
                        row = {**slm_bench.score(case, reply), "ttft_s": round(first, 4) if first else None,
                               "first_sentence_s": round(sent, 4), "total_s": round(total, 4),
                               **{k: tm.get(k) for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")}}
                    except Exception as exc:  # noqa: BLE001 - a failed case is recorded, never a crash
                        row = {"correct": False, "spoken": False, "usable": False, "reply": "", "error": str(exc)[:200]}
                    block["rows"].append({"case": case["id"], "kind": case["kind"], **row})
            finally:
                stop(proc)
        rows = block["rows"]
        block["summary"] = {"n": len(rows), "usable_n": sum(r["usable"] for r in rows),
                            "correct_n": sum(r["correct"] for r in rows), "errors": sum(bool(r.get("error")) for r in rows),
                            "first_sentence_p50_s": p50(r.get("first_sentence_s") for r in rows),
                            "ttft_p50_s": p50(r.get("ttft_s") for r in rows),
                            "total_p50_s": p50(r.get("total_s") for r in rows),
                            "prompt_ms_p50": p50(r.get("prompt_ms") for r in rows),
                            "prompt_n_p50": p50(r.get("prompt_n") for r in rows)}
        out["blocks"].append(block)
        print(json.dumps({k: v for k, v in block.items() if k != "rows"}), file=sys.stderr, flush=True)
        Path(o.out).write_text(json.dumps(out, indent=1))
        if stopped:
            break
    out["every"], out["case_ids"] = o.every, [c["id"] for c in cases]
    out["complete"] = not stopped and all(b["summary"]["n"] == len(cases) for b in out["blocks"])
    out["wall_s"] = round(time.monotonic() - t_start, 1)
    Path(o.out).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
