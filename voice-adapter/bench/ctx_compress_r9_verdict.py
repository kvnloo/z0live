"""C1-ctx-compress-r9 frozen verdict (rule in the r9 PREREG; never edited after scoring).

  python ctx_compress_r9_verdict.py job*.json > verdict.json

Jobs by meta r9_set: "main" = r2 suite (18 planted), "recency" = r6 recency suite (recency-1..3, 9 probes),
"r8" = r8 held-out suite (r8-allotment/-choir/-cycling, 21 probes). Arms: A = r4, B = r9 (production), R = r7
(report-only rival, block R1), O = full brief (report-only, block O1).

Frozen parts reused unchanged: ctx_compress_r2_verdict.verdict + ctx_compress_r5_gates.gates (main set),
ctx_compress_r6_verdict.recency (recency counts, NULL checks) and .latency, ctx_compress_r8_verdict.r8 (r8 counts,
NULL checks). band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over the 30 recency + r8 probes.

Outcome (first match):
  NULL    r2 verdict NULL, or any r6-recency / r8 NULL reason (missing/incomplete job, missing A1 B1 A2 B2 block,
          request ERROR in a primary block, wrong scenarios or per-block probe counts).
  KEEP    every gate passes:
            G1 fm_loss (PRIMARY): max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band      (r8 c_old, 3 per block)
            G2 recency_stale_fixed: max(STALE_B) < min(STALE_A) - band                       (r6 set, 6 per block)
            G3 r8a_stale_fixed: max(STALE_SAME_B) < min(STALE_SAME_A) - band                 (r8 a1 a2, 6 per block)
            G4 unique_kept: min(RECALL_B) >= max(RECALL_A) - band, for r6 glaze (3) and r8 d (3) each
            G5 main_recall: r2 verdict outcome KEEP (18 planted; min B >= max A - main band)
            G6 r5_gates: no r5 gate failure (planted SKIP/SUPERSEDED/INVENTED, unplanted WRONG/INVENTED)
            G7 no_new_invented: each B block INVENTED over the 30 recency + r8 probes <= max(A1, A2) + band
  REVERT  otherwise; `failed` names every failed gate.
Report-only: r8 REPH (b), FMNEW (c_new), O1, R1 (r7) per category, latency, flips.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_compress_r2_verdict as r2v  # noqa: E402
import ctx_compress_r5_gates as r5g  # noqa: E402
import ctx_compress_r6_verdict as v6  # noqa: E402
import ctx_compress_r8_verdict as v8  # noqa: E402

PRIMARY = ("A1", "B1", "A2", "B2")
REPORT = ("O1", "R1")
LABELS = PRIMARY + REPORT
REC_CATS = {"REC_SUP": ("bench", "badge"), "REC_UNIQ": ("glaze",)}
GATES = ("G1_fm_loss", "G2_recency_stale_fixed", "G3_r8a_stale_fixed", "G4_unique_kept", "G5_main_recall",
         "G6_r5_gates", "G7_no_new_invented")


def results(jobs, lab):
    """{(scenario, key): result} for one block label over the jobs (R1 is not in the frozen r6/r8 LABELS)."""
    out = {}
    for j in jobs:
        for b in j.get("blocks", []):
            if b["label"] == lab:
                out.update({(r["scenario"], r["key"]): r["result"] for r in b["rows"]})
    return out


def tally(items, keys):
    vals = [v for (s, k), v in items.items() if k in keys]
    return {"n": len(vals), **{c: vals.count(c) for c in v8.CLASSES}}


def per_arm(rec_jobs, r8_jobs):
    """Per label: counts per category over both sets (from the raw rows, so R1 is included)."""
    out = {}
    for lab in LABELS:
        ri, ei = results(rec_jobs, lab), results(r8_jobs, lab)
        row = {cat: tally(ri, keys) for cat, keys in REC_CATS.items()}
        row.update({f"R8_{cat}": tally(ei, keys) for cat, keys in v8.CATS.items()})
        row["INVENTED_ALL"] = sum(v == "INVENTED" for v in list(ri.values()) + list(ei.values()))
        row["n_all"] = len(ri) + len(ei)
        out[lab] = row
    return out


def verdict(jobs):
    main = [j for j in jobs if j.get("r9_set") == "main"]
    rec_jobs = [j for j in jobs if j.get("r9_set") == "recency"]
    r8_jobs = [j for j in jobs if j.get("r9_set") == "r8"]
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    rec = v6.recency(rec_jobs)
    e8 = v8.r8(r8_jobs)
    lat = v6.latency(main)
    arms = per_arm(rec_jobs, r8_jobs)
    reasons = list(v2["null_reasons"]) + rec["null_reasons"] + e8["null_reasons"]
    aa, bb = rec["aa_flips"] + e8["aa_flips"], rec["bb_flips"] + e8["bb_flips"]
    band = max(aa, bb)

    def col(cat, cls):
        return {lab: arms[lab][cat][cls] for lab in LABELS}

    loss = {lab: arms[lab]["R8_FM"]["n"] - arms[lab]["R8_FM"]["RECALL"] for lab in LABELS}
    rec_stale, r8a_stale = col("REC_SUP", "STALE"), col("R8_SAME", "STALE")
    glaze, uniq = col("REC_UNIQ", "RECALL"), col("R8_UNIQ", "RECALL")
    inv = {lab: arms[lab]["INVENTED_ALL"] for lab in LABELS}

    def mx(d, a):
        return max(d[f"{a}1"], d[f"{a}2"])

    def mn(d, a):
        return min(d[f"{a}1"], d[f"{a}2"])

    checks = {
        "G1_fm_loss": mx(loss, "B") <= mx(loss, "A") + band,
        "G2_recency_stale_fixed": mx(rec_stale, "B") < mn(rec_stale, "A") - band,
        "G3_r8a_stale_fixed": mx(r8a_stale, "B") < mn(r8a_stale, "A") - band,
        "G4_unique_kept": mn(glaze, "B") >= mx(glaze, "A") - band and mn(uniq, "B") >= mx(uniq, "A") - band,
        "G5_main_recall": v2["outcome"] == "KEEP",
        "G6_r5_gates": not g5["gate_failures"],
        "G7_no_new_invented": max(inv["B1"], inv["B2"]) <= mx(inv, "A") + band,
    }
    failed = [g for g in GATES if not checks[g]]
    outcome = "NULL" if reasons else ("KEEP" if not failed else "REVERT")
    return {"outcome": outcome, "failed": [] if reasons else failed, "checks": checks, "null_reasons": reasons,
            "band": band, "aa_flips": aa, "bb_flips": bb,
            "flips": {"recency_aa": rec["aa_flips"], "recency_bb": rec["bb_flips"], "r8_aa": e8["aa_flips"],
                      "r8_bb": e8["bb_flips"], "main_aa": v2["aa_flips"], "main_bb": v2["bb_flips"]},
            "fm_loss": loss, "recency_stale": rec_stale, "r8a_stale": r8a_stale, "glaze_recall": glaze,
            "r8d_recall": uniq, "invented_all": inv,
            "r8b_reph_stale_report_only": col("R8_REPH", "STALE"),
            "r8b_reph_correct_report_only": col("R8_REPH", "CORRECT"),
            "r8c_new_recall_report_only": col("R8_FMNEW", "RECALL"),
            "per_arm": arms, "main_r2": v2, "main_r5_gates": g5, "recency": rec, "r8": e8,
            "rival_r7_items_report_only": {"recency": {f"{s}/{k}": v for (s, k), v in sorted(results(rec_jobs, "R1").items())},
                                           "r8": {f"{s}/{k}": v for (s, k), v in sorted(results(r8_jobs, "R1").items())}},
            "latency_report_only": lat}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
