"""Score one local GGUF on frozen z0live lane suites with the vendored z0live slm_bench logic (CPU llama.cpp).

  python3 ci_bench.py --gguf /models/x.gguf --name qwen3-1.7b-Q4_K_M --suites slm-v1,slm-v2 --out result.json

Scoring is slm_bench.score/run_model/summarize unchanged (vendor/, z0live GPL-3.0). This file adds only the gate
fields Track B uses: must-SKIP passes (of 8) and an invented-fact count, plus runner facts.

invented_facts (heuristic, documented so it can be audited per row):
  a must-SKIP case answered with content (not SKIP and no refusal phrase), i.e. a claim about Kevin's screen,
  files, messages, builds, weather or a personal fact absent from the context; plus a context/followup case
  answered (not SKIP) with a wrong reply that states a number.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = Path(os.environ.get("Z0_VENDOR", str(HERE / "vendor"))).resolve()  # z0live voice-adapter/ (or a vendored copy)
sys.path.insert(0, str(VENDOR))
import slm_bench  # noqa: E402  (reads LLAMA_SERVER at import)

SUITES = {"slm-v1": VENDOR / "bench/slm-v1.json", "slm-v2": VENDOR / "bench/slm-v2.json",
          "slm-v2-model1100": HERE / "suites/slm-v2-model1100.json",
          "slm-v2-chat1k": HERE / "suites/slm-v2-chat1k.json",  # medium brief, make_medium.py (GH8)
          "slm-v2-half": HERE / "suites/slm-v2-half.json"}  # half filler brief, make_half.py (GH9)
REFUSAL = re.compile(r"(can'?t|cannot|unable|don'?t have|do not have|no access|not able|not sure|don'?t know|"
                     r"isn'?t in|not in the context|hermes)", re.I)
DIGIT = re.compile(r"\d")


def gates(suite, rows):
    by_id = {c["id"]: c for c in suite["cases"]}
    skip_rows = [r for r in rows if by_id[r["case"]].get("skip")]
    invented = []
    for r in rows:
        case, reply = by_id[r["case"]], r.get("reply", "")
        if r.get("error") or not reply:
            continue
        if case.get("skip") and not r["correct"] and not REFUSAL.search(reply):
            invented.append(r["case"])
        elif case["kind"] in ("context", "followup") and not r["correct"] and not slm_bench.tiers.SKIP.search(reply) \
                and DIGIT.search(reply):
            invented.append(r["case"])
    return {"usable_n": sum(r["usable"] for r in rows), "n": len(rows),
            "must_skip_n": sum(r["correct"] for r in skip_rows), "must_skip_total": len(skip_rows),
            "invented_facts": len(invented), "invented_cases": invented}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--suites", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", default="{}", help="JSON with matrix facts (model repo, quant, brief)")
    o = ap.parse_args()
    path = Path(o.gguf)
    out = {"schema": "z0-hillclimb.z0live-text.v1", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "name": o.name, **json.loads(o.meta), "gguf_sha256": sha256(path), "gguf_bytes": path.stat().st_size,
           "harness_sha256": {f: sha256(VENDOR / f)[:16] for f in ("slm_bench.py", "tiers.py")},
           "server": os.path.basename(slm_bench.SERVER), "llama_cpp": os.environ.get("LLAMA_TAG"),
           "runner": {"cpus": os.cpu_count(), "machine": platform.machine(),
                      "cpu": next((ln.split(":", 1)[1].strip() for ln in Path("/proc/cpuinfo").read_text().splitlines()
                                   if ln.startswith("model name")), None) if Path("/proc/cpuinfo").exists() else None},
           "suites": []}
    t0 = time.monotonic()
    proc = slm_bench.serve(path)
    out["load_s"] = round(time.monotonic() - t0, 2)
    try:
        for key in filter(None, o.suites.split(",")):
            suite = json.loads(SUITES[key].read_text())
            t1 = time.monotonic()
            rows = slm_bench.run_model(o.name, suite, slm_bench.ask_local,
                                       cold=lambda s, u: slm_bench.ask_local(s, u, cache=False))
            summary = slm_bench.summarize(o.name, rows, {}, path.stat().st_size)
            out["suites"].append({"suite": key, "suite_id": suite["suite"], "suite_sha256": sha256(SUITES[key]),
                                  "wall_s": round(time.monotonic() - t1, 1), "summary": summary,
                                  **gates(suite, rows), "rows": rows})
            print(json.dumps({k: v for k, v in out["suites"][-1].items() if k != "rows"}), file=sys.stderr, flush=True)
    finally:
        slm_bench.stop(proc)
    Path(o.out).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
