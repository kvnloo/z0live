"""C1-ctx-compress-r8 frozen verdict (rule in the r8 PREREG; never edited after scoring).

  python ctx_compress_r8_verdict.py job*.json > verdict.json

Jobs with meta r8_set == "main" are the r2 suite (workshop-1..3, 18 planted); r8_set == "r8" are the r8 suite
(r8-allotment, r8-cycling, r8-choir; 7 probes each, split over two jobs). Arms: A = r4 compressor, B = r7 production
compressor, O = full brief (report-only).

Per primary block (A1 B1 A2 B2), summed over the 3 r8 scenarios:
  SAME  (a1, a2; 6 per block)    supersede pairs in the slot pattern: CORRECT / STALE / BOTH / ABSTAIN / INVENTED / ...
  REPH  (b1, b2; 6 per block)    rephrased supersede pairs (no slot on the update): same classes, REPORT-ONLY coverage
  FM    (c_old; 3 per block)     older of two true facts sharing one lexical slot: RECALL, LOSS = 3 - RECALL
  FMNEW (c_new; 3 per block)     newer fact of the false-merge pair: RECALL, report-only
  UNIQ  (d; 3 per block)         old unique fact: RECALL
  band = max(A1 vs A2 per-item flips, B1 vs B2 per-item flips) over all 21 r8 probes (result string differs).
Main set: r2 frozen verdict (ctx_compress_r2_verdict.verdict) + r5 frozen gates (ctx_compress_r5_gates), unchanged.
Latency: ctx_compress_r6_verdict.latency over the main jobs, report-only.

Outcome (first match):
  NULL        r2 verdict NULL; an r8 job missing/incomplete; a missing A1 B1 A2 B2 block; a request ERROR in a primary
              r8 block; r8 scenarios != R8; per-block probe counts != 6/6/3/3/3.
  REVERT-r7   false merge: min(LOSS_B1, LOSS_B2) > max(LOSS_A1, LOSS_A2) + band
              (recommend not merging PR #172 as-is).
  KEEP-r7-generalises  all of:
              (a) max(STALE_SAME_B1, B2) < min(STALE_SAME_A1, A2) - band;
              (c) max(LOSS_B1, LOSS_B2) <= min(LOSS_A1, LOSS_A2) + band;
              (d) min(UNIQ_B1, B2) >= max(UNIQ_A1, A2) - band;
              main: r2 verdict outcome KEEP and r5 gates outcome != REVERT (planted recall unchanged).
  NO_UPDATE   otherwise.
Report-only: REPH counts (coverage), FMNEW, O1, latency, each KEEP condition as a boolean.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_compress_r2_verdict as r2v  # noqa: E402
import ctx_compress_r5_gates as r5g  # noqa: E402
import ctx_compress_r6_verdict as v6  # noqa: E402

PRIMARY = ("A1", "B1", "A2", "B2")
LABELS = PRIMARY + ("O1",)
R8 = ["r8-allotment", "r8-choir", "r8-cycling"]  # sorted
CATS = {"SAME": ("a1", "a2"), "REPH": ("b1", "b2"), "FM": ("c_old",), "FMNEW": ("c_new",), "UNIQ": ("d",)}
EXPECT_N = {"SAME": 6, "REPH": 6, "FM": 3, "FMNEW": 3, "UNIQ": 3}
CLASSES = ("CORRECT", "STALE", "BOTH", "RECALL", "ABSTAIN", "INVENTED", "OFF_FORMAT", "ERROR")


def r8(jobs):
    reasons = []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != R8:
        reasons.append(f"r8 scenarios {seen} != {R8}")
    items = {lab: {} for lab in LABELS}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete r8 job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing r8 block {lab} in {j.get('scenarios')}")
                continue
            for r in b["rows"]:
                if lab in PRIMARY and r["result"] == "ERROR":
                    reasons.append(f"request error in r8 {lab} {r['scenario']} {r['key']}")
                items[lab][(r["scenario"], r["key"])] = r["result"]
    counts = {}
    for lab in LABELS:
        counts[lab] = {}
        for cat, keys in CATS.items():
            vals = [v for (s, k), v in items[lab].items() if k in keys]
            counts[lab][cat] = {"n": len(vals), **{c: vals.count(c) for c in CLASSES}}
            if cat in ("FM", "FMNEW", "UNIQ"):
                counts[lab][cat]["LOSS"] = len(vals) - vals.count("RECALL")
            if lab in PRIMARY and len(vals) != EXPECT_N[cat]:
                reasons.append(f"r8 {lab} {cat}: {len(vals)} probes != {EXPECT_N[cat]}")

    def flips(x, y):
        return sum(items[x].get(k) != items[y].get(k) for k in set(items[x]) | set(items[y]))

    aa, bb = flips("A1", "A2"), flips("B1", "B2")
    return {"counts": counts, "aa_flips": aa, "bb_flips": bb, "band": max(aa, bb),
            "items": {lab: {f"{s}/{k}": v for (s, k), v in sorted(items[lab].items())} for lab in LABELS},
            "null_reasons": reasons}


def verdict(jobs):
    main = [j for j in jobs if j.get("r8_set") == "main"]
    rjobs = [j for j in jobs if j.get("r8_set") == "r8"]
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    rec = r8(rjobs)
    lat = v6.latency(main)
    c, band = rec["counts"], rec["band"]
    reasons = list(v2["null_reasons"]) + rec["null_reasons"]
    stale = {lab: c[lab]["SAME"]["STALE"] for lab in LABELS}
    loss = {lab: c[lab]["FM"]["LOSS"] for lab in LABELS}
    uniq = {lab: c[lab]["UNIQ"]["RECALL"] for lab in LABELS}
    reph_stale = {lab: c[lab]["REPH"]["STALE"] for lab in LABELS}
    reph_correct = {lab: c[lab]["REPH"]["CORRECT"] for lab in LABELS}

    def mx(d, a):
        return max(d[f"{a}1"], d[f"{a}2"])

    def mn(d, a):
        return min(d[f"{a}1"], d[f"{a}2"])

    checks = {
        "a_same_slot_stale_fixed": mx(stale, "B") < mn(stale, "A") - band,
        "c_false_merge_within_band": mx(loss, "B") <= mn(loss, "A") + band,
        "d_unique_kept": mn(uniq, "B") >= mx(uniq, "A") - band,
        "main_recall_unchanged": v2["outcome"] == "KEEP" and g5["outcome"] != "REVERT",
        "false_merge_revert": mn(loss, "B") > mx(loss, "A") + band,
    }
    if reasons:
        outcome = "NULL"
    elif checks["false_merge_revert"]:
        outcome = "REVERT-r7"
    elif all(checks[k] for k in ("a_same_slot_stale_fixed", "c_false_merge_within_band", "d_unique_kept",
                                 "main_recall_unchanged")):
        outcome = "KEEP-r7-generalises"
    else:
        outcome = "NO_UPDATE"
    return {"outcome": outcome, "checks": checks, "null_reasons": reasons, "band": band,
            "same_stale": stale, "false_merge_loss": loss, "unique_recall": uniq,
            "reph_stale_report_only": reph_stale, "reph_correct_report_only": reph_correct,
            "fm_new_recall_report_only": {lab: c[lab]["FMNEW"]["RECALL"] for lab in LABELS},
            "main_r2": v2, "main_r5_gates": g5, "r8": rec, "latency_report_only": lat}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
