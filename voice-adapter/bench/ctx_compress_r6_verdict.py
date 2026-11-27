"""C1-ctx-compress-r6 frozen verdict (rule in the r6 PREREG; never edited after scoring).

  python ctx_compress_r6_verdict.py job*.json > verdict.json

Jobs with meta r6_set == "main" are the r2 suite (workshop-1..3, 18 planted); r6_set == "recency" are the recency
suite (recency-1..3). Arms: A = r4 compressor, B = r6 compressor, O = full brief (report-only).

Main set: r2 frozen verdict (ctx_compress_r2_verdict.verdict, unchanged) + r5 frozen gates (ctx_compress_r5_gates).
Recency set, per block, summed over jobs:
  supersede probes (bench, badge; 6 per block): CORRECT / STALE / BOTH / ABSTAIN / INVENTED / OFF_FORMAT / ERROR
  unique probes (glaze; 3 per block): RECALL or not
  rec band = A/A per-item flips (A1 vs A2) over all 9 recency probes (result string differs).
Latency: first_sentence_s p50 pooled per arm over the main jobs (= r2 verdict p50); t band = |p50(A1) - p50(A2)|.

Outcome (first match):
  NULL    r2 verdict NULL; or a recency job missing/incomplete, a missing A1 B1 A2 B2 block, a request ERROR in a
          primary recency block, recency scenarios != RECENCY, or != 6 supersede / 3 unique probes per primary block.
  REVERT  r2/r5 outcome REVERT (planted recall below A beyond band, or an r5 gate);
          or B unique recall: min(B1,B2) < max(A1,A2) - rec band;
          or recency INVENTED: any B block > max(A1, A2);
          or latency: p50(B) > p50(A) + t band;
          or stale: min(B1,B2) > max(A1,A2) + rec band.
  KEEP    stale: max(B1,B2) < min(A1,A2) - rec band.
  NO_UPDATE otherwise (stale counts within the band, including A at 0).
Report-only: outcome_without_unique_gate (same rule minus the unique-recall gate).
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_compress_r2_verdict as r2v  # noqa: E402
import ctx_compress_r5_gates as r5g  # noqa: E402

PRIMARY = ("A1", "B1", "A2", "B2")
LABELS = PRIMARY + ("O1",)
RECENCY = ["recency-1", "recency-2", "recency-3"]
SUPERSEDE_KEYS = ("bench", "badge")
UNIQUE_KEYS = ("glaze",)
CLASSES = ("CORRECT", "STALE", "BOTH", "ABSTAIN", "INVENTED", "OFF_FORMAT", "ERROR")


def _p50(v):
    return round(statistics.median(v), 3) if v else None


def recency(jobs):
    reasons = []
    seen = sorted({s for j in jobs for s in j.get("scenarios", [])})
    if seen != RECENCY:
        reasons.append(f"recency scenarios {seen} != {RECENCY}")
    items = {lab: {} for lab in LABELS}
    for j in jobs:
        if not j.get("complete"):
            reasons.append(f"incomplete recency job {j.get('scenarios')}")
        blocks = {b["label"]: b for b in j.get("blocks", [])}
        for lab in LABELS:
            b = blocks.get(lab)
            if b is None:
                if lab in PRIMARY:
                    reasons.append(f"missing recency block {lab} in {j.get('scenarios')}")
                continue
            for r in b["rows"]:
                if lab in PRIMARY and r["result"] == "ERROR":
                    reasons.append(f"request error in recency {lab} {r['scenario']} {r['key']}")
                items[lab][(r["scenario"], r["key"])] = r["result"]
    counts, unique = {}, {}
    for lab in LABELS:
        sup = [v for (s, k), v in items[lab].items() if k in SUPERSEDE_KEYS]
        uni = [v for (s, k), v in items[lab].items() if k in UNIQUE_KEYS]
        counts[lab] = {"n": len(sup), **{c: sup.count(c) for c in CLASSES}}
        unique[lab] = {"n": len(uni), "RECALL": uni.count("RECALL"),
                       "INVENTED": uni.count("INVENTED"), "ABSTAIN": uni.count("ABSTAIN")}
        if lab in PRIMARY and (len(sup) != 6 or len(uni) != 3):
            reasons.append(f"recency {lab}: {len(sup)} supersede / {len(uni)} unique probes != 6 / 3")
    aa = sum(items["A1"].get(k) != items["A2"].get(k) for k in set(items["A1"]) | set(items["A2"]))
    bb = sum(items["B1"].get(k) != items["B2"].get(k) for k in set(items["B1"]) | set(items["B2"]))
    return {"counts": counts, "unique": unique, "aa_flips": aa, "bb_flips": bb, "band": aa,
            "items": {lab: {f"{s}/{k}": v for (s, k), v in sorted(items[lab].items())} for lab in LABELS},
            "null_reasons": reasons}


def latency(jobs):
    lat = {lab: [] for lab in LABELS}
    for j in jobs:
        for b in j.get("blocks", []):
            if b["label"] in lat:
                lat[b["label"]].extend(r["first_sentence_s"] for r in b["rows"] if r.get("first_sentence_s") is not None)
    per_block = {lab: _p50(v) for lab, v in lat.items()}
    arm = {a: _p50(lat[f"{a}1"] + lat[f"{a}2"]) for a in "AB"}
    arm["O"] = per_block["O1"]
    band = None if None in (per_block["A1"], per_block["A2"]) else round(abs(per_block["A1"] - per_block["A2"]), 3)
    return {"p50_block": per_block, "p50_arm": arm, "band": band}


def verdict(jobs):
    main = [j for j in jobs if j.get("r6_set") == "main"]
    rec_jobs = [j for j in jobs if j.get("r6_set") == "recency"]
    v2 = r2v.verdict(main)
    g5 = r5g.gates(v2, main)
    rec = recency(rec_jobs)
    lat = latency(main)
    c, u, band = rec["counts"], rec["unique"], rec["band"]
    stale = {lab: c[lab]["STALE"] for lab in LABELS}
    reasons, failed = list(v2["null_reasons"]) + rec["null_reasons"], []
    if g5["outcome"] == "REVERT":
        failed.append(f"r2/r5 main outcome REVERT ({'; '.join(g5['gate_failures']) or 'r2 recall rule'})")
    if not reasons:
        if min(u["B1"]["RECALL"], u["B2"]["RECALL"]) < max(u["A1"]["RECALL"], u["A2"]["RECALL"]) - band:
            failed.append(f"unique recall B {u['B1']['RECALL']},{u['B2']['RECALL']} < A max "
                          f"{max(u['A1']['RECALL'], u['A2']['RECALL'])} - band {band}")
        inv = {lab: c[lab]["INVENTED"] + u[lab]["INVENTED"] for lab in PRIMARY}
        failed += [f"recency INVENTED {lab} {inv[lab]} > A max {max(inv['A1'], inv['A2'])}"
                   for lab in ("B1", "B2") if inv[lab] > max(inv["A1"], inv["A2"])]
        if lat["band"] is None or lat["p50_arm"]["B"] is None or lat["p50_arm"]["A"] is None:
            reasons.append("latency missing")
        elif lat["p50_arm"]["B"] > lat["p50_arm"]["A"] + lat["band"]:
            failed.append(f"p50 B {lat['p50_arm']['B']} > A {lat['p50_arm']['A']} + band {lat['band']}")
        if min(stale["B1"], stale["B2"]) > max(stale["A1"], stale["A2"]) + band:
            failed.append(f"stale B {stale['B1']},{stale['B2']} > A max {max(stale['A1'], stale['A2'])} + band {band}")
    def decide(fails):
        if reasons:
            return "NULL"
        if fails:
            return "REVERT"
        if max(stale["B1"], stale["B2"]) < min(stale["A1"], stale["A2"]) - band:
            return "KEEP"
        return "NO_UPDATE"

    outcome = decide(failed)
    # report-only: the same rule without the unique-recall gate (the brief's keep rule as first written)
    no_unique = decide([f for f in failed if not f.startswith("unique recall")])
    return {"outcome": outcome, "outcome_without_unique_gate": no_unique, "failed": failed, "null_reasons": reasons,
            "stale": stale,
            "main_r2": v2, "main_r5_gates": g5, "recency": rec, "latency": lat}


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
