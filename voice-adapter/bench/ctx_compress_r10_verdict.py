"""C1-ctx-compress-r10 frozen verdict (rule in the r10 PREREG; never edited after scoring).

  python ctx_compress_r10_verdict.py job*.json > verdict.json

Jobs by meta r10_set: "main" = r2 suite (18 planted), "recency" = r6 recency suite (9 probes), "r8" = r8 held-out
suite (21 probes), "r10" = r10 held-out suite (r10-rowing/-climbing/-library, 21 probes). Arms: A = r4, B = r9
(production, unchanged), R = r7 (report-only rival, block R1), O = full brief (report-only, block O1).

Frozen parts reused unchanged: ctx_compress_r2_verdict.verdict + ctx_compress_r5_gates.gates (main set),
ctx_compress_r6_verdict.recency and .latency, ctx_compress_r8_verdict.r8, ctx_compress_r9_verdict.results / .tally.
band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over the 51 recency + r8 + r10 probes.

Presence (declared before scoring): a probe's fact is PRESENT for an arm when its planted value (probe.current) is a
substring of that arm's compressed brief (ctx_compress_r10_eval.presence(), deterministic, arms A and B). The
INVENTED gate counts only probes whose fact is present in the A or the B brief; probes absent from both are
report-only (r9's G7 counted them; see the r10 PREREG "r9 lessons").

Outcome (first match):
  NULL    r2 verdict NULL, or any recency / r8 / r10 NULL reason (missing or incomplete job, missing A1 B1 A2 B2 block,
          request ERROR in a primary block, wrong scenarios, per-block counts not as frozen), or a scored probe with
          no presence entry.
  KEEP    every gate passes:
            G1 r10_cfm_loss (PRIMARY): max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band   (r10 e1_old e2_old, 6)
            G2 r8_fm_loss: max(LOSS_B) <= max(LOSS_A) + band                                  (r8 c_old, 3)
            G3 recency_stale_fixed: max(STALE_B) < min(STALE_A) - band                        (r6 set, 6)
            G4 r8a_stale_fixed: max(STALE_B) < min(STALE_A) - band                            (r8 a1 a2, 6)
            G5 r10a_stale_fixed: max(STALE_B) < min(STALE_A) - band                           (r10 a1 a2, 6)
            G6 unique_kept: min(RECALL_B) >= max(RECALL_A) - band for r6 glaze, r8 d, r10 d (3 each)
            G7 main_recall: r2 verdict outcome KEEP
            G8 r5_gates: no r5 gate failure
            G9 no_new_invented_present: each B block INVENTED over the present-in-A-or-B probes <= max(A1, A2) + band
  REVERT  otherwise; `failed` names every failed gate.
Report-only: INVENTED over the absent-in-both probes, r10 CFMNEW (e1_new e2_new), r8 REPH / FMNEW, O1, R1, latency.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ctx_compress_r2_verdict as r2v  # noqa: E402
import ctx_compress_r5_gates as r5g  # noqa: E402
import ctx_compress_r6_verdict as v6  # noqa: E402
import ctx_compress_r8_verdict as v8  # noqa: E402
import ctx_compress_r9_verdict as v9  # noqa: E402

PRIMARY = ("A1", "B1", "A2", "B2")
REPORT = ("O1", "R1")
LABELS = PRIMARY + REPORT
R10 = ["r10-climbing", "r10-library", "r10-rowing"]  # sorted
R10_CATS = {"SAME": ("a1", "a2"), "CFM": ("e1_old", "e2_old"), "CFMNEW": ("e1_new", "e2_new"), "UNIQ": ("d",)}
R10_EXPECT_N = {"SAME": 6, "CFM": 6, "CFMNEW": 6, "UNIQ": 3}
CLASSES = v8.CLASSES
GATES = ("G1_r10_cfm_loss", "G2_r8_fm_loss", "G3_recency_stale_fixed", "G4_r8a_stale_fixed", "G5_r10a_stale_fixed",
         "G6_unique_kept", "G7_main_recall", "G8_r5_gates", "G9_no_new_invented_present")


def load_presence():
    """Frozen presence map from the harness (imports the compressors; needs tiers' deps)."""
    import ctx_compress_r10_eval as r10e  # noqa: E402
    return r10e.presence()


def r10(jobs):
    """r10 per-block counts and NULL checks (same structure as ctx_compress_r8_verdict.r8)."""
    reasons = []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != R10:
        reasons.append(f"r10 scenarios {seen} != {R10}")
    items = {lab: {} for lab in LABELS}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete r10 job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing r10 block {lab} in {j.get('scenarios')}")
                continue
            for r in b["rows"]:
                if lab in PRIMARY and r["result"] == "ERROR":
                    reasons.append(f"request error in r10 {lab} {r['scenario']} {r['key']}")
                items[lab][(r["scenario"], r["key"])] = r["result"]
    counts = {}
    for lab in LABELS:
        counts[lab] = {}
        for cat, keys in R10_CATS.items():
            vals = [v for (s, k), v in items[lab].items() if k in keys]
            counts[lab][cat] = {"n": len(vals), **{c: vals.count(c) for c in CLASSES}}
            if cat in ("CFM", "CFMNEW", "UNIQ"):
                counts[lab][cat]["LOSS"] = len(vals) - vals.count("RECALL")
            if lab in PRIMARY and len(vals) != R10_EXPECT_N[cat]:
                reasons.append(f"r10 {lab} {cat}: {len(vals)} probes != {R10_EXPECT_N[cat]}")

    def flips(x, y):
        return sum(items[x].get(k) != items[y].get(k) for k in set(items[x]) | set(items[y]))

    aa, bb = flips("A1", "A2"), flips("B1", "B2")
    return {"counts": counts, "aa_flips": aa, "bb_flips": bb, "band": max(aa, bb),
            "items": {lab: {f"{s}/{k}": v for (s, k), v in sorted(items[lab].items())} for lab in LABELS},
            "null_reasons": reasons}


def invented_split(probe_jobs, pres):
    """Per label: INVENTED over probes present in the A or B brief (gated) and over probes absent from both
    (report-only). Also the item lists, so the split is auditable."""
    gated, absent, missing = {}, {}, set()
    for lab in LABELS:
        res = v9.results(probe_jobs, lab)
        g = a = 0
        for (s, k), v in res.items():
            p = pres.get(f"{s}/{k}")
            if p is None:
                missing.add(f"{s}/{k}")
                continue
            if any(p.values()):
                g += v == "INVENTED"
            else:
                a += v == "INVENTED"
        gated[lab], absent[lab] = g, a
    return gated, absent, sorted(missing)


def verdict(jobs, pres=None):
    pres = load_presence() if pres is None else pres
    main = [j for j in jobs if j.get("r10_set") == "main"]
    rec_jobs = [j for j in jobs if j.get("r10_set") == "recency"]
    r8_jobs = [j for j in jobs if j.get("r10_set") == "r8"]
    r10_jobs = [j for j in jobs if j.get("r10_set") == "r10"]
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    rec = v6.recency(rec_jobs)
    e8 = v8.r8(r8_jobs)
    e10 = r10(r10_jobs)
    lat = v6.latency(main)
    arms9 = v9.per_arm(rec_jobs, r8_jobs)
    c10 = e10["counts"]
    probe_jobs = rec_jobs + r8_jobs + r10_jobs
    inv_present, inv_absent, missing = invented_split(probe_jobs, pres)
    reasons = list(v2["null_reasons"]) + rec["null_reasons"] + e8["null_reasons"] + e10["null_reasons"]
    reasons += [f"no presence entry for {k}" for k in missing]
    aa = rec["aa_flips"] + e8["aa_flips"] + e10["aa_flips"]
    bb = rec["bb_flips"] + e8["bb_flips"] + e10["bb_flips"]
    band = max(aa, bb)

    def col9(cat, cls):
        return {lab: arms9[lab][cat][cls] for lab in LABELS}

    def col10(cat, cls):
        return {lab: c10[lab][cat][cls] for lab in LABELS}

    cfm_loss = col10("CFM", "LOSS")
    r8_loss = {lab: arms9[lab]["R8_FM"]["n"] - arms9[lab]["R8_FM"]["RECALL"] for lab in LABELS}
    rec_stale, r8a_stale, r10a_stale = col9("REC_SUP", "STALE"), col9("R8_SAME", "STALE"), col10("SAME", "STALE")
    glaze, r8d, r10d = col9("REC_UNIQ", "RECALL"), col9("R8_UNIQ", "RECALL"), col10("UNIQ", "RECALL")

    def mx(d, a):
        return max(d[f"{a}1"], d[f"{a}2"])

    def mn(d, a):
        return min(d[f"{a}1"], d[f"{a}2"])

    checks = {
        "G1_r10_cfm_loss": mx(cfm_loss, "B") <= mx(cfm_loss, "A") + band,
        "G2_r8_fm_loss": mx(r8_loss, "B") <= mx(r8_loss, "A") + band,
        "G3_recency_stale_fixed": mx(rec_stale, "B") < mn(rec_stale, "A") - band,
        "G4_r8a_stale_fixed": mx(r8a_stale, "B") < mn(r8a_stale, "A") - band,
        "G5_r10a_stale_fixed": mx(r10a_stale, "B") < mn(r10a_stale, "A") - band,
        "G6_unique_kept": all(mn(d, "B") >= mx(d, "A") - band for d in (glaze, r8d, r10d)),
        "G7_main_recall": v2["outcome"] == "KEEP",
        "G8_r5_gates": not g5["gate_failures"],
        "G9_no_new_invented_present": mx(inv_present, "B") <= mx(inv_present, "A") + band,
    }
    failed = [g for g in GATES if not checks[g]]
    outcome = "NULL" if reasons else ("KEEP" if not failed else "REVERT")
    n_present = sum(any(v.values()) for v in pres.values())
    return {"outcome": outcome, "failed": [] if reasons else failed, "checks": checks, "null_reasons": reasons,
            "band": band, "aa_flips": aa, "bb_flips": bb,
            "flips": {"recency_aa": rec["aa_flips"], "recency_bb": rec["bb_flips"], "r8_aa": e8["aa_flips"],
                      "r8_bb": e8["bb_flips"], "r10_aa": e10["aa_flips"], "r10_bb": e10["bb_flips"],
                      "main_aa": v2["aa_flips"], "main_bb": v2["bb_flips"]},
            "r10_cfm_loss": cfm_loss, "r8_fm_loss": r8_loss, "recency_stale": rec_stale, "r8a_stale": r8a_stale,
            "r10a_stale": r10a_stale, "r10a_correct": col10("SAME", "CORRECT"),
            "glaze_recall": glaze, "r8d_recall": r8d, "r10d_recall": r10d,
            "invented_present": inv_present, "invented_absent_report_only": inv_absent,
            "presence_counts": {"probes": len(pres), "present_in_A_or_B": n_present,
                                "absent_in_both": len(pres) - n_present,
                                "absent_in_both_keys": sorted(k for k, v in pres.items() if not any(v.values()))},
            "r10_cfm_new_recall_report_only": col10("CFMNEW", "RECALL"),
            "r8b_reph_stale_report_only": col9("R8_REPH", "STALE"),
            "r8c_new_recall_report_only": col9("R8_FMNEW", "RECALL"),
            "per_arm_r9_sets": arms9, "r10": e10, "main_r2": v2, "main_r5_gates": g5, "recency": rec, "r8": e8,
            "latency_report_only": lat}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
