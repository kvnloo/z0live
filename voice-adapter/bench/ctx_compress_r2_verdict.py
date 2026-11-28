"""C1-ctx-compress-r2 frozen verdict (rule in the r2 PREREG; never edited after scoring).

  python ctx_compress_r2_verdict.py job*.json > verdict.json

Each job JSON is one ctx_compress_r2_eval.py run (one scenario probe half, blocks A1 B1 A2 B2 O1 on one runner).
Primary (C1 rule, unchanged): per-block recall = RECALL over planted probes summed over jobs; band = A/A flips
(A1 vs A2); KEEP iff min(B1, B2) >= max(A1, A2) - band, else REVERT.
NULL: job missing/incomplete, a primary block missing, a request error in a primary block, scenarios != EXPECTED,
or a primary block without all 18 planted probes.
Report-only: brief-region recall (studio/locker/vendor) per block; discrimination check = O1 recall < min(A1, A2)
(the pre-r2 tail-cut compressor loses planted recall, i.e. the test can see a dropped fact); B/B flips;
first_sentence p50 pooled per arm. O1 never changes the outcome.
"""
import json
import statistics
import sys

EXPECTED = ["workshop-1", "workshop-2", "workshop-3"]
PRIMARY = ("A1", "B1", "A2", "B2")
LABELS = PRIMARY + ("O1",)
PLANTED = 18
BRIEF_KEYS = ("studio", "locker", "vendor")


def recalled(block):
    return {(r["scenario"], r["key"]): r["result"] == "RECALL" for r in block["rows"] if r["current"] is not None}


def verdict(jobs):
    reasons = []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != EXPECTED:
        reasons.append(f"scenarios {seen} != {EXPECTED}")
    per = {lab: {} for lab in LABELS}
    lat = {"A": [], "B": [], "O": []}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing block {lab} in {j.get('scenarios')}")
                continue
            if lab in PRIMARY and any(r["result"] == "ERROR" for r in b["rows"]):
                reasons.append(f"request error in {lab} {j.get('scenarios')}")
            per[lab].update(recalled(b))
            lat[lab[0]].extend(r["first_sentence_s"] for r in b["rows"] if r.get("first_sentence_s") is not None)
    recall = {lab: sum(per[lab].values()) for lab in LABELS}
    planted = {lab: len(per[lab]) for lab in LABELS}
    brief = {lab: sum(v for (s, k), v in per[lab].items() if k in BRIEF_KEYS) for lab in LABELS}
    if any(planted[lab] != PLANTED for lab in PRIMARY):
        reasons.append(f"planted per block {planted} != {PLANTED}")

    def flips(x, y):
        return sum(per[x].get(k) != per[y].get(k) for k in set(per[x]) | set(per[y]))

    aa, bb = flips("A1", "A2"), flips("B1", "B2")
    band = aa
    p50 = {a: round(statistics.median(v), 3) if v else None for a, v in lat.items()}
    if reasons:
        outcome = "NULL"
    elif min(recall["B1"], recall["B2"]) >= max(recall["A1"], recall["A2"]) - band:
        outcome = "KEEP"
    else:
        outcome = "REVERT"
    disc = None if planted["O1"] != PLANTED else recall["O1"] < min(recall["A1"], recall["A2"])
    return {"outcome": outcome, "recall": recall, "planted": planted, "brief_region_recall": brief,
            "aa_flips": aa, "bb_flips": bb, "band": band, "discriminates_O1_lt_A": disc,
            "first_sentence_p50_s": p50, "null_reasons": reasons}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
