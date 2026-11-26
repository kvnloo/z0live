"""C1-ctx-compress frozen verdict (rule in bench/ctx-compress-c1-PREREG.md; never edited after scoring).

  python ctx_compress_verdict.py job0.json job1.json job2.json > verdict.json

Each job JSON is one ctx_compress_eval.py run (one scenario, ABAB blocks A1 B1 A2 B2 on one runner).
Per-block recall = RECALL count over planted probes, summed over jobs. A/A flips = planted probes whose RECALL
status differs between A1 and A2, summed over jobs; band = A/A flips. B/B flips (B1 vs B2) are reported only.
NULL: a job missing/incomplete, a block missing, any request error, or the jobs do not cover EXPECTED scenarios.
KEEP: min(B1, B2) >= max(A1, A2) - band. Otherwise REVERT. Latency is secondary (pooled first_sentence p50).
"""
import json
import statistics
import sys

EXPECTED = ["workshop-1", "workshop-2", "workshop-3"]
LABELS = ("A1", "B1", "A2", "B2")


def recalled(block):
    return {(r["scenario"], r["key"]): r["result"] == "RECALL" for r in block["rows"] if r["current"] is not None}


def verdict(jobs):
    reasons = []
    seen = sorted(s for j in jobs for s in j.get("scenarios", []))
    if seen != EXPECTED:
        reasons.append(f"scenarios {seen} != {EXPECTED}")
    per = {lab: {} for lab in LABELS}
    lat = {"A": [], "B": []}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                reasons.append(f"missing block {lab} in {j.get('scenarios')}")
                continue
            if any(r["result"] == "ERROR" for r in b["rows"]):
                reasons.append(f"request error in {lab} {j.get('scenarios')}")
            per[lab].update(recalled(b))
            lat[lab[0]].extend(r["first_sentence_s"] for r in b["rows"] if r.get("first_sentence_s") is not None)
    recall = {lab: sum(per[lab].values()) for lab in LABELS}
    planted = {lab: len(per[lab]) for lab in LABELS}

    def flips(x, y):
        return sum(per[x].get(k) != per[y].get(k) for k in set(per[x]) | set(per[y]))

    aa, bb = flips("A1", "A2"), flips("B1", "B2")
    band = aa  # B/B flips are reported, not used: a noisy B arm must not widen its own band
    p50 = {a: round(statistics.median(v), 3) if v else None for a, v in lat.items()}
    if reasons:
        outcome = "NULL"
    elif min(recall["B1"], recall["B2"]) >= max(recall["A1"], recall["A2"]) - band:
        outcome = "KEEP"
    else:
        outcome = "REVERT"
    return {"outcome": outcome, "recall": recall, "planted": planted, "aa_flips": aa, "bb_flips": bb,
            "band": band, "first_sentence_p50_s": p50, "null_reasons": reasons}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
