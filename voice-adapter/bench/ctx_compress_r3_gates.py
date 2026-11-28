"""C1-ctx-compress-r3 frozen gate check (rule in the r3 PREREG; never edited after scoring).

  python ctx_compress_r3_gates.py verdict.json job*.json > gates.json

Outcome = r2 frozen verdict (ctx_compress_r2_verdict.py, unchanged) with three gates on top: for each of
SKIP, SUPERSEDED, INVENTED, the count in each B block (summed over jobs, all probes) must be <= the max over A1/A2.
A failed gate turns KEEP into REVERT; NULL and REVERT pass through unchanged.
"""
import json
import sys

GATES = ("SKIP", "SUPERSEDED", "INVENTED")


def gates(verdict, jobs):
    cnt = {lab: {g: 0 for g in GATES} for lab in ("A1", "B1", "A2", "B2")}
    for j in jobs:
        for b in j.get("blocks", []):
            if b["label"] in cnt:
                for r in b["rows"]:
                    if r["result"] in GATES:
                        cnt[b["label"]][r["result"]] += 1
    failed = [f"{g} {lab} {cnt[lab][g]} > A max {max(cnt['A1'][g], cnt['A2'][g])}"
              for g in GATES for lab in ("B1", "B2") if cnt[lab][g] > max(cnt["A1"][g], cnt["A2"][g])]
    outcome = verdict["outcome"]
    if outcome == "KEEP" and failed:
        outcome = "REVERT"
    return {"outcome": outcome, "verdict_outcome": verdict["outcome"], "counts": cnt, "gate_failures": failed}


if __name__ == "__main__":
    v = json.load(open(sys.argv[1]))
    print(json.dumps(gates(v, [json.load(open(p)) for p in sys.argv[2:]]), indent=1))
