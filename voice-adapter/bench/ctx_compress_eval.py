"""C1-ctx-compress: planted-fact context recall, full brief (A) vs brief_compress.compress_brief(brief, 2810) (B).

  LLAMA_SERVER=.../llama-server python ctx_compress_eval.py --gguf m.gguf --blocks A,B,A,B --scenarios 0 --out r.json
  python ctx_compress_eval.py --dry      # no model: context chars per arm and which planted values each arm holds

Cases, probes and the scorer are context_eval.py's, unchanged (context_eval.suite / context_eval.score; recall =
RECALL count over planted probes, context_eval.summarize's definition). The only per-arm difference is the chat brief:
  A = context_eval.snapshot(scenario)                                   (tiers.snapshot(brief, call turns))
  B = tiers.snapshot(compress_brief(brief, 2810), call turns)            (same brief text, compressed)
System prompt = tiers.SYSTEM.format(tier="fast") + "\\n\\n" + context, as context_eval and slm_bench send it.
Transport, server flags and sampling are lanespeed.py's (fresh llama-server per block, greedy, seed 42, max 120
tokens, thinking off, cache_prompt false), so latency is comparable with the C56 runs. Synthetic text only.
"""
import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import context_eval  # noqa: E402
import lanespeed  # noqa: E402
import slm_bench  # noqa: E402
import tiers  # noqa: E402

BUDGET = 2810  # = r4 arm-B budget (len(slm-v2-e5 brief))


def chat_brief(scenario):
    # identical text to the brief inside context_eval.snapshot (test asserts arm A is unchanged)
    return scenario.stable + "\n" + "\n".join(f"{r}: {t}" for r, t in context_eval.exchange(scenario.chat))


def context(scenario, arm):
    if arm == "A":
        return context_eval.snapshot(scenario)
    if arm == "B":
        return tiers.snapshot(brief_compress.compress_brief(chat_brief(scenario), BUDGET),
                              context_eval.exchange(scenario.call))
    raise ValueError(arm)


def system(scenario, arm):
    return tiers.SYSTEM.format(tier="fast") + "\n\n" + context(scenario, arm)


def select(scenarios, probes):
    # AMEND-1 (infra): split a scenario's probes over two jobs so each job fits the 45 min timeout
    if not probes:
        return scenarios
    idx = [int(i) for i in probes.split(",")]
    return [dataclasses.replace(s, probes=tuple(s.probes[i] for i in idx)) for s in scenarios]


def recall_n(rows):
    return sum(r["result"] == "RECALL" for r in rows if r["current"] is not None)


def dry(scenarios):
    out = []
    for s in scenarios:
        row = {"scenario": s.name, "brief_chars": len(chat_brief(s))}
        for arm in "AB":
            ctx = context(s, arm)
            row[arm] = {"system_chars": len(system(s, arm)),
                        "planted_present": [p.key for p in s.probes if p.current and p.current in ctx]}
        out.append(row)
    return out


def run_block(scenarios, arm, t_start, deadline_s):
    rows, stopped = [], False
    first = scenarios[0]
    lanespeed.ask(system(first, arm), first.probes[0].ask)  # unscored warm-up
    for s in scenarios:
        sysmsg = system(s, arm)
        for p in s.probes:
            if time.monotonic() - t_start > deadline_s:
                return rows, True
            row = {"scenario": s.name, **{k: v for k, v in vars(p).items()}}
            try:
                text, ttft, sent, total, tm = lanespeed.ask(sysmsg, p.ask)
                reply = slm_bench.THINK.sub("", text).strip()
                row.update({"reply": reply, "result": context_eval.score(p, reply),
                            "ttft_s": round(ttft, 4) if ttft else None, "first_sentence_s": round(sent, 4),
                            "total_s": round(total, 4),
                            **{k: tm.get(k) for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")}})
            except Exception as exc:  # noqa: BLE001 - a failed request is recorded, never a crash
                row.update({"reply": "", "result": "ERROR", "error": str(exc)[:200]})
            rows.append(row)
    return rows, stopped


def summary(rows):
    planted = [r for r in rows if r["current"] is not None]
    return {"n": len(rows), "planted_n": len(planted), "recall_n": recall_n(rows),
            "errors": sum(r["result"] == "ERROR" for r in rows),
            "results": {k: sum(r["result"] == k for r in rows)
                        for k in ("RECALL", "SUPERSEDED", "INVENTED", "SKIP", "OFF_FORMAT", "ERROR")},
            "first_sentence_p50_s": lanespeed.p50(r.get("first_sentence_s") for r in rows),
            "ttft_p50_s": lanespeed.p50(r.get("ttft_s") for r in rows),
            "prompt_n_p50": lanespeed.p50(r.get("prompt_n") for r in rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf")
    ap.add_argument("--blocks", default="A,B,A,B")
    ap.add_argument("--scenarios", default="0,1,2", help="context_eval.suite() indices")
    ap.add_argument("--probes", default=None, help="AMEND-1: probe indices per scenario, e.g. 0,1,2,3 (default all)")
    ap.add_argument("--ub", type=int, default=512)
    ap.add_argument("--deadline-s", type=float, default=2100)
    ap.add_argument("--meta", default="{}")
    ap.add_argument("--out")
    ap.add_argument("--logdir", default=".")
    ap.add_argument("--dry", action="store_true")
    o = ap.parse_args()
    suite = context_eval.suite()
    scenarios = select([suite[int(i)] for i in o.scenarios.split(",")], o.probes)
    if o.dry:
        print(json.dumps(dry(scenarios), indent=1))
        return 0
    server, gguf = os.environ["LLAMA_SERVER"], Path(o.gguf)
    files = ("ctx_compress_eval.py", "context_eval.py", "lanespeed.py", "eval_local.py")
    out = {"schema": "z0live.ctx-compress.v1", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           **json.loads(o.meta), "gguf_sha256": lanespeed.sha256(gguf), "budget_chars": BUDGET,
           "scenarios": [s.name for s in scenarios], "dry": dry(scenarios),
           "harness_sha256": {**{f: lanespeed.sha256(HERE / f) for f in files},
                              **{f: lanespeed.sha256(HERE.parent / f)
                                 for f in ("brief_compress.py", "tiers.py", "slm_bench.py")}},
           "complete": False, "blocks": []}
    t_start, stopped = time.monotonic(), False
    for bi, arm in enumerate(filter(None, o.blocks.split(","))):
        label = f"{arm}{sum(b['arm'] == arm for b in out['blocks']) + 1}"
        with open(Path(o.logdir) / f"server-{bi}-{label}.log", "w") as log:
            t0 = time.monotonic()
            proc, cmd = lanespeed.serve(server, gguf, o.ub, log)
            block = {"block": bi, "label": label, "arm": arm, "cmd": [os.path.basename(cmd[0])] + cmd[1:],
                     "load_s": round(time.monotonic() - t0, 2)}
            try:
                block["rows"], stopped = run_block(scenarios, arm, t_start, o.deadline_s)
            finally:
                lanespeed.stop(proc)
        block["summary"] = summary(block["rows"])
        out["blocks"].append(block)
        print(json.dumps({"label": label, **block["summary"]}), file=sys.stderr, flush=True)
        Path(o.out).write_text(json.dumps(out, indent=1))
        if stopped:
            break
    expected = sum(len(s.probes) for s in scenarios)
    out["complete"] = not stopped and all(b["summary"]["n"] == expected for b in out["blocks"])
    out["wall_s"] = round(time.monotonic() - t_start, 1)
    Path(o.out).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
