"""C1-ctx-compress-r14 frozen verdict (rule in the r14 PREREG; never edited after scoring).

  python ctx_compress_r14_verdict.py job*.json > verdict.json

Jobs by meta r10_set: "main" = r2 suite (18 planted, blocks B A B A O), "r14" = r14 held-out suite (r14-airport /
-ski / -theatre, 27 probes, blocks B A B A O R), and the dev / re-run sets, blocks B A B A, gated probes (+ the r13
report-only limit items): "r13" (a2x a2y old, e1 e2, f, a3_old, e3: 21), "r12" (a-d old, e1 e2, f: 21), "recency"
(bench badge glaze: 9), "r8" (a1 a2 d c_old: 12), "r10" (a1 a2 d e1_old e2_old: 15). Every job must carry meta
r14_harness == "ctx-compress-r14". Arms: A = r4 (baseline), B = r14 (production, label tie-break on attribute key v2),
R = r13 (report-only rival, block R1 on r14), O = full brief (report-only, O1 on r14).

Frozen parts reused unchanged: ctx_compress_r2_verdict.verdict + ctx_compress_r5_gates.gates (main set),
ctx_compress_r6_verdict.latency, ctx_compress_r9_verdict.results, ctx_compress_r8_verdict.CLASSES.
band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over every scored r14 / r13 / r12 / recency / r8 / r10 probe.
Presence: ctx_compress_r14_eval.presence() (planted value is a substring of the A / B brief); INVENTED is gated over
scored probes present in the A or the B brief, report-only over probes absent from both.

Outcome (first match):
  NULL    r2 verdict NULL; for any set: scenarios != its 3 names, incomplete job, missing A1 B1 A2 B2 block, request
          ERROR in a primary block, per-block category counts not as frozen; a scored probe with no presence entry; a
          job without r14_harness meta; no jobs.
  KEEP    every gate passes:
            G1  r14_a3_loss (PRIMARY): max(LOSS_B) <= max(LOSS_A) + band over r14 a3x_old a3y_old (6 per block)
            G1e r14_e3_stale (PRIMARY): max(STALE_B) < min(STALE_A) - band over r14 e3x e3y (6 per block)
            G1b r13_fm_loss (dev): max(LOSS_B) <= max(LOSS_A) + band over r13 a2x_old a2y_old (6)
            G1c r12_fm_loss (dev): same over r12 a_old b_old c_old d_old (12)
            G1d rerun_fm_loss: same over r8 c_old + r10 e1_old e2_old (9)
            G2  stale_fixed, EVERY set: max(STALE_B) < min(STALE_A) - band for r14 e1 e2, r13 e1 e2, r12 e1 e2,
                recency bench badge, r8 a1 a2, r10 a1 a2 (6 each)
            G3  unique_kept, every set: min(RECALL_B) >= max(RECALL_A) - band for r14 f, r13 f, r12 f, recency glaze,
                r8 d, r10 d (3 each)
            G4  main_recall: r2 verdict outcome KEEP (18 planted)
            G5  r5_gates: no r5 gate failure
            G6  no_new_invented_present: max(INVENTED_B) <= max(INVENTED_A) + band over scored probes present in A or B
            G7  latency: p50 first sentence (main) B <= A + max(latency band, 5% of A)  (band = |p50 A1 - p50 A2|)
  REVERT  otherwise; `failed` names every failed gate.
Report-only: r13 a3_old LOSS and r13 e3 STALE / CORRECT (the v2 limits, predicted fixed by r14 on the set they were
found on), r14 newer-fact recall (a3x_new a3y_new), r14 e3 CORRECT, r12 (a) LOSS alone, INVENTED over absent-in-both
probes, O1, R1.
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

HARNESS = "ctx-compress-r14"
PRIMARY = ("A1", "B1", "A2", "B2")
REPORT = ("O1", "R1")
LABELS = PRIMARY + REPORT
CLASSES = v8.CLASSES
SETS = {
    "r14": {"names": ["r14-airport", "r14-ski", "r14-theatre"],
            "cats": {"A3": ("a3x_old", "a3y_old"), "E3": ("e3x", "e3y"), "SAME": ("e1", "e2"), "UNIQ": ("f",),
                     "A3NEW": ("a3x_new", "a3y_new")}},
    "r13": {"names": ["r13-festival", "r13-hotel", "r13-marina"],
            "cats": {"FM": ("a2x_old", "a2y_old"), "SAME": ("e1", "e2"), "UNIQ": ("f",), "A3": ("a3_old",),
                     "E3": ("e3",)}},
    "r12": {"names": ["r12-campsite", "r12-coworking", "r12-leisure"],
            "cats": {"FM": ("a_old", "b_old", "c_old", "d_old"), "FM_CO": ("a_old",), "SAME": ("e1", "e2"),
                     "UNIQ": ("f",)}},
    "recency": {"names": ["recency-1", "recency-2", "recency-3"],
                "cats": {"SAME": ("bench", "badge"), "UNIQ": ("glaze",)}},
    "r8": {"names": ["r8-allotment", "r8-choir", "r8-cycling"],
           "cats": {"SAME": ("a1", "a2"), "UNIQ": ("d",), "FM": ("c_old",)}},
    "r10": {"names": ["r10-climbing", "r10-library", "r10-rowing"],
            "cats": {"SAME": ("a1", "a2"), "UNIQ": ("d",), "FM": ("e1_old", "e2_old")}},
}
LOSS_CATS = ("FM", "FM_CO", "UNIQ", "A3", "A3NEW")
PROBE_SETS = tuple(SETS)
GATES = ("G1_r14_a3_loss", "G1e_r14_e3_stale", "G1b_r13_fm_loss", "G1c_r12_fm_loss", "G1d_rerun_fm_loss",
         "G2_stale_fixed", "G3_unique_kept", "G4_main_recall", "G5_r5_gates", "G6_no_new_invented_present",
         "G7_latency")
ARMS = {"A": "r4 (bench/brief_compress_r4.py)", "B": "r14 label tie-break on attribute key v2 (brief_compress.py)",
        "R": "r13 attribute key v2 (bench/brief_compress_r13.py, report-only)", "O": "full brief (report-only)"}


def expect_n(which, cat):
    return 3 * len(SETS[which]["cats"][cat])


def load_presence():
    import ctx_compress_r14_eval as r14e  # noqa: E402
    return r14e.presence()


def per_set(which, jobs):
    spec, reasons = SETS[which], []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != spec["names"]:
        reasons.append(f"{which} scenarios {seen} != {spec['names']}")
    items = {lab: {} for lab in LABELS}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete {which} job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing {which} block {lab} in {j.get('scenarios')}")
                continue
            for r in b["rows"]:
                if lab in PRIMARY and r["result"] == "ERROR":
                    reasons.append(f"request error in {which} {lab} {r['scenario']} {r['key']}")
                items[lab][(r["scenario"], r["key"])] = r["result"]
    counts = {}
    for lab in LABELS:
        counts[lab] = {}
        for cat, keys in spec["cats"].items():
            vals = [v for (s, k), v in items[lab].items() if k in keys]
            counts[lab][cat] = {"n": len(vals), **{c: vals.count(c) for c in CLASSES}}
            if cat in LOSS_CATS:
                counts[lab][cat]["LOSS"] = len(vals) - vals.count("RECALL")
            if lab in PRIMARY and len(vals) != expect_n(which, cat):
                reasons.append(f"{which} {lab} {cat}: {len(vals)} probes != {expect_n(which, cat)}")

    def flips(x, y):
        return sum(items[x].get(k) != items[y].get(k) for k in set(items[x]) | set(items[y]))

    return {"counts": counts, "aa_flips": flips("A1", "A2"), "bb_flips": flips("B1", "B2"),
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
    by_set = {w: [j for j in jobs if j.get("r10_set") == w] for w in PROBE_SETS}
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    e = {w: per_set(w, by_set[w]) for w in PROBE_SETS}
    lat = v6.latency(main)
    probe_jobs = [j for w in PROBE_SETS for j in by_set[w]]
    inv_present, inv_absent, missing = invented_split(probe_jobs, pres)
    reasons = list(v2["null_reasons"]) + [r for w in PROBE_SETS for r in e[w]["null_reasons"]]
    reasons += [f"no presence entry for {k}" for k in missing]
    bad = sorted({str(j.get("scenarios")) + "/" + str(j.get("r10_set")) for j in jobs
                  if j.get("r14_harness") != HARNESS})
    reasons += [f"job without r14_harness meta: {b}" for b in bad]
    if not jobs:
        reasons.append("no jobs")
    aa = sum(e[w]["aa_flips"] for w in PROBE_SETS)
    bb = sum(e[w]["bb_flips"] for w in PROBE_SETS)
    band = max(aa, bb)

    def col(w, cat, cls):
        return {lab: e[w]["counts"][lab][cat][cls] for lab in LABELS}

    def mx(d, a):
        return max(d[f"{a}1"], d[f"{a}2"])

    def mn(d, a):
        return min(d[f"{a}1"], d[f"{a}2"])

    def add(*ds):
        return {lab: sum(d[lab] for d in ds) for lab in LABELS}

    a3, e3 = col("r14", "A3", "LOSS"), col("r14", "E3", "STALE")
    fm13, fm12 = col("r13", "FM", "LOSS"), col("r12", "FM", "LOSS")
    fm_rerun = add(col("r8", "FM", "LOSS"), col("r10", "FM", "LOSS"))
    stale = {w: col(w, "SAME", "STALE") for w in PROBE_SETS}
    uniq = {w: col(w, "UNIQ", "RECALL") for w in PROBE_SETS}
    pa, pb, lband = lat["p50_arm"].get("A"), lat["p50_arm"].get("B"), lat["band"]
    lat_ok = None not in (pa, pb, lband) and pb <= pa + max(lband, 0.05 * pa)
    stale_ok = {w: mx(stale[w], "B") < mn(stale[w], "A") - band for w in PROBE_SETS}
    uniq_ok = {w: mn(uniq[w], "B") >= mx(uniq[w], "A") - band for w in PROBE_SETS}
    checks = {
        "G1_r14_a3_loss": mx(a3, "B") <= mx(a3, "A") + band,
        "G1e_r14_e3_stale": mx(e3, "B") < mn(e3, "A") - band,
        "G1b_r13_fm_loss": mx(fm13, "B") <= mx(fm13, "A") + band,
        "G1c_r12_fm_loss": mx(fm12, "B") <= mx(fm12, "A") + band,
        "G1d_rerun_fm_loss": mx(fm_rerun, "B") <= mx(fm_rerun, "A") + band,
        "G2_stale_fixed": all(stale_ok.values()),
        "G3_unique_kept": all(uniq_ok.values()),
        "G4_main_recall": v2["outcome"] == "KEEP",
        "G5_r5_gates": not g5["gate_failures"],
        "G6_no_new_invented_present": mx(inv_present, "B") <= mx(inv_present, "A") + band,
        "G7_latency": bool(lat_ok),
    }
    failed = [g for g in GATES if not checks[g]]
    outcome = "NULL" if reasons else ("KEEP" if not failed else "REVERT")
    scored = {f"{s}/{k}" for w in PROBE_SETS for lab in PRIMARY for (s, k) in v9.results(by_set[w], lab)}
    sp = {k: pres[k] for k in sorted(scored) if k in pres}
    n_present = sum(any(v.values()) for v in sp.values())
    return {"experiment": "C1-ctx-compress-r14", "arms": ARMS, "outcome": outcome,
            "failed": [] if reasons else failed, "checks": checks, "null_reasons": reasons,
            "band": band, "aa_flips": aa, "bb_flips": bb,
            "flips": {**{f"{w}_aa": e[w]["aa_flips"] for w in PROBE_SETS},
                      **{f"{w}_bb": e[w]["bb_flips"] for w in PROBE_SETS},
                      "main_aa": v2.get("aa_flips"), "main_bb": v2.get("bb_flips")},
            "r14_a3_loss": a3, "r14_e3_stale": e3, "r14_e3_correct_report_only": col("r14", "E3", "CORRECT"),
            "r13_fm_loss": fm13, "r12_fm_loss": fm12, "rerun_fm_loss": fm_rerun,
            "stale": stale, "stale_ok": stale_ok, "same_correct": {w: col(w, "SAME", "CORRECT") for w in PROBE_SETS},
            "unique_recall": uniq, "unique_ok": uniq_ok,
            "invented_present": inv_present, "invented_absent_report_only": inv_absent,
            "r14_a3_new_recall_report_only": col("r14", "A3NEW", "RECALL"),
            "r13_a3_loss_report_only": col("r13", "A3", "LOSS"), "r13_e3_stale_report_only": col("r13", "E3", "STALE"),
            "r13_e3_correct_report_only": col("r13", "E3", "CORRECT"),
            "r12_a_loss_report_only": col("r12", "FM_CO", "LOSS"),
            "presence_counts": {"scored_probes": len(sp), "present_in_A_or_B": n_present,
                                "absent_in_both": len(sp) - n_present,
                                "absent_in_both_keys": sorted(k for k, v in sp.items() if not any(v.values()))},
            "latency": lat, "sets": e, "main_r2": v2, "main_r5_gates": g5}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
