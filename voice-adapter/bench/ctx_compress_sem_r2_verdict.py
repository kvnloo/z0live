"""C1-ctx-sem-r2 frozen verdict (rule in the sem-r2 PREREG; never edited after scoring). Same as the sem-r1 verdict
except: harness ctx-compress-sem-r2, arm B = sem-r2 (cached per-statement keys + collision judge + displacement rule),
R = sem-r1 (report-only), G8 replaced by G8a (brief-time added wall with a warm cache, p50 <= 3.0 s, no fallback and
no model-call error in any B block) and G8b (per-new-statement arrival cost, report-only), plus report-only ablation
presence (B-nojudge, B-nodisp) and R1 presence.

  python ctx_compress_sem_r2_verdict.py job*.json > verdict.json

(sem-r1 docstring kept below for the shared parts; read "sem-r1" as "sem-r2" for B and G8 as G8a.)

Jobs by meta r10_set: "main" = r2 suite (18 planted, blocks B A B A O), "r15" = BLIND held-out suite (r15-vet / -car /
-pottery, 25 probes, blocks B A B A O R, two jobs per scenario), and the dev / no-regression sets, blocks B A B A, gated
probes only: "r14" (a3x a3y old, e3x e3y e1 e2, f: 21), "r13" (a2x a2y old, e1 e2, f: 15), "r12" (a-d old, e1 e2, f: 21),
"recency" (bench badge glaze: 9), "r8" (a1 a2 d c_old: 12). Every job must carry meta sem_harness ==
"ctx-compress-sem-r1". Arms: A = r4 (baseline), B = sem-r1 (r4 order + one model-keyed supersession pass, live against
the block's llama-server), R = r14 (lexical, report-only, R1 on r15), O = full brief (report-only, O1 on main and r15).

Frozen parts reused unchanged: ctx_compress_r2_verdict.verdict + ctx_compress_r5_gates.gates (main set),
ctx_compress_r6_verdict.latency, ctx_compress_r9_verdict.results, ctx_compress_r8_verdict.CLASSES.
band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over every scored r15 / r14 / r13 / r12 / recency / r8 probe.
Presence: A from ctx_compress_sem_r1_eval.presence() (offline, r4); B from the rows themselves (`in_brief`: the planted
value is in the brief the B block really used, any of B1 / B2). INVENTED is gated over scored probes present in A or B.
r15 categories come from the frozen data file (ctx_compress_r15_blind.json, per scenario / probe).
Semantic pass telemetry: every row of a B block carries `sem` (its scenario's pass: wall_s, fallback, candidates, grouped,
keys). One sample = one distinct (job, B block label, scenario).

Outcome (first match):
  NULL    r2 verdict NULL; for any set: scenarios != its 3 names, incomplete job, missing A1 B1 A2 B2 block, request
          ERROR in a primary block, per-block category counts not as frozen; a scored probe with no presence entry; a
          B row without `sem` / `in_brief`; a job without sem_harness meta; no jobs.
  KEEP    every gate passes:
            G1  r15_two_true_loss (PRIMARY): max(LOSS_B) <= max(LOSS_A) + band over r15 TWO_OLD (3 per block)
            G1e r15_update_fixed (PRIMARY): max(STALE_B) <= min(STALE_A) - 3 - band over r15 UPD (9), and
                min(CORRECT_B) >= 3
            G1s r15_static_recall: min(RECALL_B) >= max(RECALL_A) - band over r15 STATIC (11)
            G1b r14_a3_loss (dev): max(LOSS_B) <= max(LOSS_A) + band over r14 a3x_old a3y_old (6)
            G1c dev_fm_loss: same over r13 a2x_old a2y_old + r12 a-d old + r8 c_old (21)
            G2  stale_no_worse, every dev set: max(STALE_B) <= max(STALE_A) + band for r14 e3x e3y e1 e2 (12), r13 e1
                e2, r12 e1 e2, recency bench badge, r8 a1 a2 (6 each)
            G3  unique_kept, every dev set: min(RECALL_B) >= max(RECALL_A) - band for r14 f, r13 f, r12 f, glaze, r8 d
            G4  main_recall: r2 verdict outcome KEEP (18 planted)
            G5  r5_gates: no r5 gate failure
            G6  no_new_invented_present: max(INVENTED_B) <= max(INVENTED_A) + band over scored probes present in A or B
            G7  latency: p50 first sentence (main) B <= A + max(latency band, 5% of A)  (band = |p50 A1 - p50 A2|)
            G8  sem_latency: semantic pass p50 wall seconds per brief <= SEM_P50_MAX_S (3.0), no fallback in any B block
  REVERT  otherwise; `failed` names every failed gate.
Report-only: r15 STALE / CORRECT split by UPD_SAME / UPD_RENAMED and by slot form, r15 TWO_NEW recall, O1, R1, INVENTED
over absent-in-both probes, semantic pass p95 / mean / per set / key disagreement B1 vs B2.
"""
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ctx_compress_r2_verdict as r2v  # noqa: E402
import ctx_compress_r5_gates as r5g  # noqa: E402
import ctx_compress_r6_verdict as v6  # noqa: E402
import ctx_compress_r8_verdict as v8  # noqa: E402
import ctx_compress_r9_verdict as v9  # noqa: E402

HARNESS = "ctx-compress-sem-r2"
PRIMARY = ("A1", "B1", "A2", "B2")
REPORT = ("O1", "R1")
LABELS = PRIMARY + REPORT
CLASSES = v8.CLASSES
SEM_P50_MAX_S = 3.0
UPD_MIN_FIXED = 3
BLIND = json.loads((HERE / "ctx_compress_r15_blind.json").read_text())
R15_CATS = ("UPD_SAME", "UPD_RENAMED", "TWO_OLD", "TWO_NEW", "STATIC")


def r15_cats():
    """category -> sorted "<scenario>/<key>" of the r15 probes (from the frozen data file)."""
    return {c: tuple(sorted(f"{s['name']}/{p['key']}" for s in BLIND["scenarios"] for p in s["probes"]
                            if p["cat"] == c)) for c in R15_CATS}


def r15_slot_cats():
    """report-only: updates by the slot form of their two statements (from the frozen data file)."""
    out = {}
    for s in BLIND["scenarios"]:
        for p in s["probes"]:
            if p["cat"].startswith("UPD"):
                out.setdefault("SLOT_" + p["slot"].split(" ")[0].upper(), []).append(f"{s['name']}/{p['key']}")
    return {k: tuple(sorted(v)) for k, v in sorted(out.items())}


_C = r15_cats()
SETS = {
    "r15": {"names": sorted(s["name"] for s in BLIND["scenarios"]),
            "cats": {**_C, "UPD": tuple(sorted(_C["UPD_SAME"] + _C["UPD_RENAMED"])), **r15_slot_cats()}},
    "r14": {"names": ["r14-airport", "r14-ski", "r14-theatre"],
            "cats": {"A3": ("a3x_old", "a3y_old"), "SAME": ("e3x", "e3y", "e1", "e2"), "UNIQ": ("f",)}},
    "r13": {"names": ["r13-festival", "r13-hotel", "r13-marina"],
            "cats": {"FM": ("a2x_old", "a2y_old"), "SAME": ("e1", "e2"), "UNIQ": ("f",)}},
    "r12": {"names": ["r12-campsite", "r12-coworking", "r12-leisure"],
            "cats": {"FM": ("a_old", "b_old", "c_old", "d_old"), "SAME": ("e1", "e2"), "UNIQ": ("f",)}},
    "recency": {"names": ["recency-1", "recency-2", "recency-3"],
                "cats": {"SAME": ("bench", "badge"), "UNIQ": ("glaze",)}},
    "r8": {"names": ["r8-allotment", "r8-choir", "r8-cycling"],
           "cats": {"SAME": ("a1", "a2"), "UNIQ": ("d",), "FM": ("c_old",)}},
}
LOSS_CATS = ("FM", "UNIQ", "A3", "TWO_OLD", "TWO_NEW", "STATIC")
PROBE_SETS = tuple(SETS)
DEV_SETS = ("r14", "r13", "r12", "recency", "r8")
GATES = ("G1_r15_two_true_loss", "G1e_r15_update_fixed", "G1s_r15_static_recall", "G1b_r14_a3_loss",
         "G1c_dev_fm_loss", "G2_stale_no_worse", "G3_unique_kept", "G4_main_recall", "G5_r5_gates",
         "G6_no_new_invented_present", "G7_latency", "G8a_brief_latency")
ARMS = {"A": "r4 (bench/brief_compress_r4.py)",
        "B": "sem-r2: r4 order + cached per-statement keys + collision judge + static-displacement rule "
             "(brief_compress_sem2.py, live model calls, same gguf)",
        "R": "sem-r1 one batched keying pass per brief (brief_compress_sem.py, live, report-only)",
        "O": "full brief (report-only)"}
SEM_FIELDS = ("brief_wall_s", "fallback", "candidates", "collisions", "replaced", "reserved", "key_misses",
              "judge_misses", "cold_total_s", "judge_in_path_s", "arrival_walls", "cache_errors")
ABLATIONS = ("nojudge", "nodisp")


def _match(which, s, k, keys):
    return f"{s}/{k}" in keys if which == "r15" else k in keys


def expect_n(which, cat):
    keys = SETS[which]["cats"][cat]
    return len(keys) if which == "r15" else 3 * len(keys)


def load_presence():
    import ctx_compress_sem_r2_eval as ev  # noqa: E402
    return ev.presence()


def per_set(which, jobs):
    spec, reasons = SETS[which], []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != spec["names"]:
        reasons.append(f"{which} scenarios {seen} != {spec['names']}")
    items = {lab: {} for lab in LABELS}
    b_present, sem_rows, sem_keys = {}, {}, {}
    abl_present = {a: {} for a in ABLATIONS}
    r_present = {}
    for ji, j in enumerate(jobs):
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
                if lab == "R1" and r.get("in_brief") is not None:
                    r_present[f"{r['scenario']}/{r['key']}"] = bool(r["in_brief"])
                if lab in ("B1", "B2"):
                    if "sem" not in r or "in_brief" not in r or "in_brief_ablation" not in r:
                        reasons.append(f"{which} {lab} row without sem / in_brief: {r['scenario']} {r['key']}")
                        continue
                    if r["in_brief"] is not None:
                        k = f"{r['scenario']}/{r['key']}"
                        b_present[k] = b_present.get(k, False) or bool(r["in_brief"])
                        for a in ABLATIONS:
                            v = r["in_brief_ablation"].get(a)
                            abl_present[a][k] = abl_present[a].get(k, False) or bool(v)
                    sem_rows[(ji, lab, r["scenario"])] = {"set": which, "label": lab, "scenario": r["scenario"],
                                                           **{k: r["sem"].get(k) for k in SEM_FIELDS}}
                    sem_keys[(ji, r["scenario"], lab)] = r["sem"].get("keys")
    counts = {}
    for lab in LABELS:
        counts[lab] = {}
        for cat, keys in spec["cats"].items():
            vals = [v for (s, k), v in items[lab].items() if _match(which, s, k, keys)]
            counts[lab][cat] = {"n": len(vals), **{c: vals.count(c) for c in CLASSES}}
            if cat in LOSS_CATS:
                counts[lab][cat]["LOSS"] = len(vals) - vals.count("RECALL")
            if lab in PRIMARY and len(vals) != expect_n(which, cat):
                reasons.append(f"{which} {lab} {cat}: {len(vals)} probes != {expect_n(which, cat)}")

    def flips(x, y):
        return sum(items[x].get(k) != items[y].get(k) for k in set(items[x]) | set(items[y]))

    pairs = {(ji, s) for ji, s, _ in sem_keys}
    key_flips = sum(sem_keys.get((ji, s, "B1")) != sem_keys.get((ji, s, "B2")) for ji, s in pairs)
    return {"counts": counts, "aa_flips": flips("A1", "A2"), "bb_flips": flips("B1", "B2"),
            "items": {lab: {f"{s}/{k}": v for (s, k), v in sorted(items[lab].items())} for lab in LABELS},
            "b_present": b_present, "ablation_present": abl_present, "r1_present": r_present,
            "sem_rows": list(sem_rows.values()), "sem_key_disagree_B1_B2": key_flips, "null_reasons": reasons}


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


def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 3)


def sem_latency(rows):
    """G8a: brief-time added wall (warm cache) per distinct (job, B block, brief). G8b (report-only): per NEW statement
    arrival cost (key call + judge call if its key collides), pooled; cold total per brief; judge-in-path per brief."""
    walls = [r["brief_wall_s"] for r in rows if r["brief_wall_s"] is not None]
    fb = [r for r in rows if r["fallback"]]
    err = [r for r in rows if r["cache_errors"]]
    sets = sorted({r["set"] for r in rows})
    arr = [w for r in rows for w in (r["arrival_walls"] or [])]
    cold = [r["cold_total_s"] for r in rows if r["cold_total_s"] is not None]
    jip = [r["judge_in_path_s"] for r in rows if r["judge_in_path_s"] is not None]

    def p(xs):
        return {"n": len(xs), "p50_s": _pct(xs, 0.5), "p95_s": _pct(xs, 0.95), "max_s": max(xs, default=None),
                "mean_s": round(statistics.mean(xs), 3) if xs else None}
    return {"G8a": {**p(walls), "budget_p50_s": SEM_P50_MAX_S, "fallbacks": len(fb),
                    "fallback_reasons": sorted({str(r["fallback"]) for r in fb}), "rows_with_model_errors": len(err),
                    "model_errors": sorted({e for r in err for e in r["cache_errors"]})[:20],
                    "key_misses": sum(r["key_misses"] or 0 for r in rows),
                    "judge_misses": sum(r["judge_misses"] or 0 for r in rows),
                    "p50_by_set": {w: _pct([r["brief_wall_s"] for r in rows if r["set"] == w
                                            and r["brief_wall_s"] is not None], 0.5) for w in sets}},
            "G8b_report_only": {"per_new_statement": p(arr), "cold_total_per_brief": p(cold),
                                "judge_in_path_per_brief": p(jip)},
            "candidates_p50": _pct([r["candidates"] for r in rows if r["candidates"] is not None], 0.5),
            "collisions_p50": _pct([r["collisions"] for r in rows if r["collisions"] is not None], 0.5),
            "replaced_p50": _pct([r["replaced"] for r in rows if r["replaced"] is not None], 0.5),
            "reserved_p50": _pct([r["reserved"] for r in rows if r["reserved"] is not None], 0.5)}


def verdict(jobs, pres=None):
    pres = load_presence() if pres is None else pres
    main = [j for j in jobs if j.get("r10_set") == "main"]
    by_set = {w: [j for j in jobs if j.get("r10_set") == w] for w in PROBE_SETS}
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    e = {w: per_set(w, by_set[w]) for w in PROBE_SETS}
    lat = v6.latency(main)
    probe_jobs = [j for w in PROBE_SETS for j in by_set[w]]
    pres = {k: dict(v) for k, v in pres.items()}
    for w in PROBE_SETS:
        for k, v in e[w]["b_present"].items():
            if k in pres:
                pres[k]["B"] = v
    inv_present, inv_absent, missing = invented_split(probe_jobs, pres)
    sem_rows = [r for w in PROBE_SETS for r in e[w]["sem_rows"]]
    for ji, j in enumerate(main):   # the main jobs' B blocks are semantic-pass samples too
        for b in j.get("blocks", []):
            if b["label"] in ("B1", "B2"):
                seen = set()
                for r in b["rows"]:
                    if "sem" in r and r["scenario"] not in seen:
                        seen.add(r["scenario"])
                        sem_rows.append({"set": "main", "label": b["label"], "scenario": r["scenario"],
                                         **{k: r["sem"].get(k) for k in SEM_FIELDS}})
    sem_lat = sem_latency(sem_rows)
    reasons = list(v2["null_reasons"]) + [r for w in PROBE_SETS for r in e[w]["null_reasons"]]
    reasons += [f"no presence entry for {k}" for k in missing]
    bad = sorted({str(j.get("scenarios")) + "/" + str(j.get("r10_set")) for j in jobs
                  if j.get("sem_harness") != HARNESS})
    reasons += [f"job without sem_harness meta: {b}" for b in bad]
    if not jobs:
        reasons.append("no jobs")
    if not sem_rows:
        reasons.append("no semantic pass samples")
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

    two, upd, static = col("r15", "TWO_OLD", "LOSS"), col("r15", "UPD", "STALE"), col("r15", "STATIC", "RECALL")
    upd_ok = col("r15", "UPD", "CORRECT")
    a3 = col("r14", "A3", "LOSS")
    fm_dev = add(col("r13", "FM", "LOSS"), col("r12", "FM", "LOSS"), col("r8", "FM", "LOSS"))
    stale = {w: col(w, "SAME", "STALE") for w in DEV_SETS}
    uniq = {w: col(w, "UNIQ", "RECALL") for w in DEV_SETS}
    pa, pb, lband = lat["p50_arm"].get("A"), lat["p50_arm"].get("B"), lat["band"]
    lat_ok = None not in (pa, pb, lband) and pb <= pa + max(lband, 0.05 * pa)
    g8a = sem_lat["G8a"]
    sem_ok = (g8a["p50_s"] is not None and g8a["p50_s"] <= SEM_P50_MAX_S and g8a["fallbacks"] == 0
              and g8a["rows_with_model_errors"] == 0)
    stale_ok = {w: mx(stale[w], "B") <= mx(stale[w], "A") + band for w in DEV_SETS}
    uniq_ok = {w: mn(uniq[w], "B") >= mx(uniq[w], "A") - band for w in DEV_SETS}
    checks = {
        "G1_r15_two_true_loss": mx(two, "B") <= mx(two, "A") + band,
        "G1e_r15_update_fixed": mx(upd, "B") <= mn(upd, "A") - UPD_MIN_FIXED - band and mn(upd_ok, "B") >= UPD_MIN_FIXED,
        "G1s_r15_static_recall": mn(static, "B") >= mx(static, "A") - band,
        "G1b_r14_a3_loss": mx(a3, "B") <= mx(a3, "A") + band,
        "G1c_dev_fm_loss": mx(fm_dev, "B") <= mx(fm_dev, "A") + band,
        "G2_stale_no_worse": all(stale_ok.values()),
        "G3_unique_kept": all(uniq_ok.values()),
        "G4_main_recall": v2["outcome"] == "KEEP",
        "G5_r5_gates": not g5["gate_failures"],
        "G6_no_new_invented_present": mx(inv_present, "B") <= mx(inv_present, "A") + band,
        "G7_latency": bool(lat_ok),
        "G8a_brief_latency": bool(sem_ok),
    }
    failed = [g for g in GATES if not checks[g]]
    outcome = "NULL" if reasons else ("KEEP" if not failed else "REVERT")
    scored = {f"{s}/{k}" for w in PROBE_SETS for lab in PRIMARY for (s, k) in v9.results(by_set[w], lab)}
    sp = {k: pres[k] for k in sorted(scored) if k in pres}
    n_present = sum(any(v.values()) for v in sp.values())
    split = {c: col("r15", c, "STALE") for c in ("UPD_SAME", "UPD_RENAMED") + tuple(r15_slot_cats())}
    cats15 = SETS["r15"]["cats"]

    def present_by_cat(d):
        return {c: sum(bool(d.get(k)) for k in cats15[c]) for c in ("UPD", "TWO_OLD", "STATIC", "TWO_NEW")}
    presence_report = {"B": present_by_cat(e["r15"]["b_present"]),
                       **{f"B_{a}": present_by_cat(e["r15"]["ablation_present"][a]) for a in ABLATIONS},
                       "R1_sem_r1": present_by_cat(e["r15"]["r1_present"]),
                       "n": {c: len(cats15[c]) for c in ("UPD", "TWO_OLD", "STATIC", "TWO_NEW")}}
    return {"experiment": "C1-ctx-sem-r2", "arms": ARMS, "outcome": outcome,
            "r15_current_value_in_brief_report_only": presence_report,
            "failed": [] if reasons else failed, "checks": checks, "null_reasons": reasons,
            "band": band, "aa_flips": aa, "bb_flips": bb,
            "flips": {**{f"{w}_aa": e[w]["aa_flips"] for w in PROBE_SETS},
                      **{f"{w}_bb": e[w]["bb_flips"] for w in PROBE_SETS},
                      "main_aa": v2.get("aa_flips"), "main_bb": v2.get("bb_flips")},
            "r15_two_true_loss": two, "r15_update_stale": upd, "r15_update_stale_split": split,
            "r15_update_correct": upd_ok,
            "r15_update_correct_split": {c: col("r15", c, "CORRECT") for c in split},
            "r15_static_recall": static, "r15_two_new_recall_report_only": col("r15", "TWO_NEW", "RECALL"),
            "r14_a3_loss": a3, "dev_fm_loss": fm_dev,
            "stale": stale, "stale_ok": stale_ok, "same_correct": {w: col(w, "SAME", "CORRECT") for w in DEV_SETS},
            "unique_recall": uniq, "unique_ok": uniq_ok,
            "invented_present": inv_present, "invented_absent_report_only": inv_absent,
            "presence_counts": {"scored_probes": len(sp), "present_in_A_or_B": n_present,
                                "absent_in_both": len(sp) - n_present,
                                "absent_in_both_keys": sorted(k for k, v in sp.items() if not any(v.values()))},
            "sem_latency": sem_lat,
            "sem_key_disagree_B1_B2": {w: e[w]["sem_key_disagree_B1_B2"] for w in PROBE_SETS},
            "latency": lat, "sets": {w: {k: v for k, v in e[w].items() if k != "sem_rows"} for w in PROBE_SETS},
            "main_r2": v2, "main_r5_gates": g5}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
