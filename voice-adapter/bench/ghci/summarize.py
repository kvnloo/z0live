"""Merge per-job result JSONs into one markdown table + summary.json.  python3 summarize.py results/ out/"""
import json
import sys
from pathlib import Path

BASELINE = {  # measured on the Studio (Metal), z0live docs/HILLCLIMB-OVERNIGHT.md Track B (B9/B12/B13)
    ("qwen3-1.7b", "Q4_K_M", "slm-v1"): "22/30", ("qwen3-1.7b", "Q4_K_M", "slm-v2"): "21/30",
    ("qwen3-1.7b", "Q4_K_M", "slm-v2-model1100"): "20/30", ("qwen3-0.6b", "Q4_K_M", "slm-v1"): "18/30",
    ("gemma4-e2b", "Q4_K_M", "slm-v2"): "25/30 skip 6/8 (quant n/r)", ("gemma4-e2b", "Q8_0", "slm-v2"): "25/30 skip 6/8 (quant n/r)",
    ("gemma4-e2b", "Q4_K_M", "slm-v2-model1100"): "24/30 skip 4/8 (quant n/r)",
    ("gemma4-e2b", "Q8_0", "slm-v2-model1100"): "24/30 skip 4/8 (quant n/r)",
}


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in sorted(src.rglob("*.json")):
        d = json.loads(f.read_text())
        if d.get("schema") != "z0-hillclimb.z0live-text.v1":
            continue
        for s in d["suites"]:
            m = s["summary"]
            rows.append({"model": d["model"], "quant": d["quant"], "brief": d["brief"], "suite": s["suite"],
                         "usable": f"{s['usable_n']}/{s['n']}", "must_skip": f"{s['must_skip_n']}/{s['must_skip_total']}",
                         "invented": s["invented_facts"], "invented_cases": s["invented_cases"],
                         "by_kind": m["by_kind"], "warm_ttft_p50_s": m["first_p50_s"], "cold_ttft_s": m["cold_first_s"],
                         "tok_s_p50": m["tok_s_p50"], "errors": m["errors"], "wall_s": s["wall_s"],
                         "studio_baseline": BASELINE.get((d["model"], d["quant"], s["suite"]), ""),
                         "gguf_sha256": d["gguf_sha256"][:12], "cpus": d["runner"]["cpus"]})
    rows.sort(key=lambda r: (r["suite"], r["model"], r["quant"]))
    head = ["suite", "model", "quant", "usable", "must_skip", "invented", "warm_ttft_p50_s", "cold_ttft_s", "tok_s_p50",
            "errors", "studio_baseline", "gguf_sha256", "cpus"]
    lines = ["## z0live SLM lane suites on GitHub CPU (llama.cpp)", "",
             "usable = correct and spoken (z0live slm_bench). must_skip of 8. invented = heuristic count (ci_bench.py). "
             "TTFT = first visible token on this runner (warm = brief in prompt cache).", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(r[h]) if r[h] is not None else "-" for h in head) + " |" for r in rows]
    lines += ["", "by_kind / invented cases:", ""]
    lines += [f"- {r['suite']} {r['model']} {r['quant']}: {r['by_kind']} invented={r['invented_cases']}" for r in rows]
    (dst / "summary.md").write_text("\n".join(lines) + "\n")
    (dst / "summary.json").write_text(json.dumps(rows, indent=1))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
