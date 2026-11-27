"""C1-ctx-compress-r12 frozen verdict (rule in the r12 PREREG; never edited after scoring).

  python ctx_compress_r12_verdict.py job*.json > verdict.json

Jobs by meta r10_set: "main" = r2 suite (18 planted), "r12" = r12 held-out suite (r12-campsite / -coworking /
-leisure, 33 probes). Every job must carry meta r12_harness == "ctx-compress-r12". Arms: A = r4 (baseline),
B = r11 (production), R = r7 (report-only rival, block R1), O = full brief (report-only, block O1).

Frozen parts reused unchanged: ctx_compress_r2_verdict.verdict + ctx_compress_r5_gates.gates (main set),
ctx_compress_r6_verdict.latency, ctx_compress_r9_verdict.results, ctx_compress_r8_verdict.CLASSES, and the per-block
counting / NULL structure of ctx_compress_r10_verdict.r10.
band = max(A1 vs A2 flips, B1 vs B2 flips) over the 33 r12 probes.
Presence: ctx_compress_r12_eval.presence() (planted value is a substring of the A / B brief); INVENTED is gated over
r12 probes present in the A or the B brief, report-only over probes absent from both.

Outcome (first match):
  NULL    r2 verdict NULL, or any r12 NULL reason (scenarios != the 3 r12 names, incomplete job, missing A1 B1 A2 B2
          block, request ERROR in a primary block, per-block category counts not as frozen), a scored r12 probe with
          no presence entry, a job without r12_harness meta, or no jobs.
  KEEP    every gate passes:
            G1 r12_fm_loss (PRIMARY): max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band
               (old-fact loss on (a)-(d): a_old b_old c_old d_old, 12 per block; LOSS = not RECALL)
            G2 r12_stale_fixed: max(STALE_B) < min(STALE_A) - band                    (e1 e2, 6 per block)
            G3 unique_kept: min(RECALL_B) >= max(RECALL_A) - band                     (f, 3 per block)
            G4 main_recall: r2 verdict outcome KEEP                                    (18 planted)
            G5 r5_gates: no r5 gate failure
            G6 no_new_invented_present: max(INVENTED_B) <= max(INVENTED_A) + band over present-in-A-or-B r12 probes
            G7 latency: p50 first sentence (main) B <= A + max(latency band, 5% of A)  (band = |p50 A1 - p50 A2|)
  REVERT  otherwise; `failed` names every failed gate.
Report-only: LOSS per category (a = CO, b-d = PRON + MULTI + CUEVAL), newer-fact recall (a_new..d_new), INVENTED over
absent-in-both probes, O1, R1.
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

HARNESS = "ctx-compress-r12"
PRIMARY = ("A1", "B1", "A2", "B2")
REPORT = ("O1", "R1")
LABELS = PRIMARY + REPORT
R12 = ["r12-campsite", "r12-coworking", "r12-leisure"]  # sorted
CATS = {"FM": ("a_old", "b_old", "c_old", "d_old"), "FM_CO": ("a_old",), "FM_BCD": ("b_old", "c_old", "d_old"),
        "FMNEW": ("a_new", "b_new", "c_new", "d_new"), "SAME": ("e1", "e2"), "UNIQ": ("f",)}
EXPECT_N = {"FM": 12, "FM_CO": 3, "FM_BCD": 9, "FMNEW": 12, "SAME": 6, "UNIQ": 3}
LOSS_CATS = ("FM", "FM_CO", "FM_BCD", "FMNEW", "UNIQ")
CLASSES = v8.CLASSES
GATES = ("G1_r12_fm_loss", "G2_r12_stale_fixed", "G3_unique_kept", "G4_main_recall", "G5_r5_gates",
         "G6_no_new_invented_present", "G7_latency")
ARMS = {"A": "r4 (bench/brief_compress_r4.py)", "B": "r11 (brief_compress.py)",
        "R": "r7 (bench/brief_compress_r7.py, report-only)", "O": "full brief (report-only)"}


def load_presence():
    import ctx_compress_r12_eval as r12e  # noqa: E402
    return r12e.presence()


def r12(jobs):
    reasons = []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != R12:
        reasons.append(f"r12 scenarios {seen} != {R12}")
    items = {lab: {} for lab in LABELS}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete r12 job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing r12 block {lab} in {j.get('scenarios')}")
                continue
            for r in b["rows"]:
                if lab in PRIMARY and r["result"] == "ERROR":
                    reasons.append(f"request error in r12 {lab} {r['scenario']} {r['key']}")
                items[lab][(r["scenario"], r["key"])] = r["result"]
    counts = {}
    for lab in LABELS:
        counts[lab] = {}
        for cat, keys in CATS.items():
            vals = [v for (s, k), v in items[lab].items() if k in keys]
            counts[lab][cat] = {"n": len(vals), **{c: vals.count(c) for c in CLASSES}}
            if cat in LOSS_CATS:
                counts[lab][cat]["LOSS"] = len(vals) - vals.count("RECALL")
            if lab in PRIMARY and len(vals) != EXPECT_N[cat]:
                reasons.append(f"r12 {lab} {cat}: {len(vals)} probes != {EXPECT_N[cat]}")

    def flips(x, y):
        return sum(items[x].get(k) != items[y].get(k) for k in set(items[x]) | set(items[y]))

    aa, bb = flips("A1", "A2"), flips("B1", "B2")
    return {"counts": counts, "aa_flips": aa, "bb_flips": bb, "band": max(aa, bb),
            "items": {lab: {f"{s}/{k}": v for (s, k), v in sorted(items[lab].items())} for lab in LABELS},
            "null_reasons": reasons}


def invented_split(jobs, pres):
    gated, absent, missing = {}, {}, set()
    for lab in LABELS:
        g = a = 0
        for (s, k), v in v9.results(jobs, lab).items():
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
    r12_jobs = [j for j in jobs if j.get("r10_set") == "r12"]
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    e12 = r12(r12_jobs)
    lat = v6.latency(main)
    c = e12["counts"]
    inv_present, inv_absent, missing = invented_split(r12_jobs, pres)
    reasons = list(v2["null_reasons"]) + e12["null_reasons"] + [f"no presence entry for {k}" for k in missing]
    bad = sorted({str(j.get("scenarios")) + "/" + str(j.get("r10_set")) for j in jobs
                  if j.get("r12_harness") != HARNESS})
    reasons += [f"job without r12_harness meta: {b}" for b in bad]
    if not jobs:
        reasons.append("no jobs")
    band = e12["band"]

    def col(cat, cls):
        return {lab: c[lab][cat][cls] for lab in LABELS}

    def mx(d, a):
        return max(d[f"{a}1"], d[f"{a}2"])

    def mn(d, a):
        return min(d[f"{a}1"], d[f"{a}2"])

    fm_loss, stale, uniq = col("FM", "LOSS"), col("SAME", "STALE"), col("UNIQ", "RECALL")
    pa, pb, lband = lat["p50_arm"].get("A"), lat["p50_arm"].get("B"), lat["band"]
    lat_ok = None not in (pa, pb, lband) and pb <= pa + max(lband, 0.05 * pa)
    checks = {
        "G1_r12_fm_loss": mx(fm_loss, "B") <= mx(fm_loss, "A") + band,
        "G2_r12_stale_fixed": mx(stale, "B") < mn(stale, "A") - band,
        "G3_unique_kept": mn(uniq, "B") >= mx(uniq, "A") - band,
        "G4_main_recall": v2["outcome"] == "KEEP",
        "G5_r5_gates": not g5["gate_failures"],
        "G6_no_new_invented_present": mx(inv_present, "B") <= mx(inv_present, "A") + band,
        "G7_latency": bool(lat_ok),
    }
    failed = [g for g in GATES if not checks[g]]
    outcome = "NULL" if reasons else ("KEEP" if not failed else "REVERT")
    n_present = sum(any(v.values()) for v in pres.values())
    return {"experiment": "C1-ctx-compress-r12", "arms": ARMS, "outcome": outcome,
            "failed": [] if reasons else failed, "checks": checks, "null_reasons": reasons,
            "band": band, "aa_flips": e12["aa_flips"], "bb_flips": e12["bb_flips"],
            "flips": {"r12_aa": e12["aa_flips"], "r12_bb": e12["bb_flips"],
                      "main_aa": v2.get("aa_flips"), "main_bb": v2.get("bb_flips")},
            "r12_fm_loss": fm_loss, "r12_stale": stale, "r12_same_correct": col("SAME", "CORRECT"),
            "r12_unique_recall": uniq, "invented_present": inv_present,
            "invented_absent_report_only": inv_absent,
            "fm_loss_co_report_only": col("FM_CO", "LOSS"), "fm_loss_bcd_report_only": col("FM_BCD", "LOSS"),
            "fm_new_recall_report_only": col("FMNEW", "RECALL"),
            "presence_counts": {"probes": len(pres), "present_in_A_or_B": n_present,
                                "absent_in_both": len(pres) - n_present,
                                "absent_in_both_keys": sorted(k for k, v in pres.items() if not any(v.values()))},
            "latency": lat, "r12": e12, "main_r2": v2, "main_r5_gates": g5}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
