"""Apply the frozen C56 decision rule (docs in the PREREG) to one lanespeed.py receipt. Prints JSON.

  python lanespeed_verdict.py receipt.json
"""
import json
import statistics
import sys


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def main():
    r = json.load(open(sys.argv[1]))
    blocks = {b["label"]: b for b in r["blocks"]}
    need = ["A1", "B1", "A2", "B2"]
    if not r.get("complete") or any(k not in blocks for k in need) or \
            any(blocks[k]["summary"]["errors"] for k in need):
        print(json.dumps({"verdict": "NULL", "reason": "incomplete blocks or request errors",
                          "blocks": {k: b["summary"] for k, b in blocks.items()}}))
        return
    fs = {k: [row["first_sentence_s"] for row in blocks[k]["rows"]] for k in need}
    p = {k: med(v) for k, v in fs.items()}
    a_p50, b_p50 = med(fs["A1"] + fs["A2"]), med(fs["B1"] + fs["B2"])
    aa = abs(p["A1"] - p["A2"])
    bb = abs(p["B1"] - p["B2"])
    diff = b_p50 - a_p50
    ordered = max(p["B1"], p["B2"]) < min(p["A1"], p["A2"])
    use = {k: blocks[k]["summary"]["usable_n"] for k in need}
    quality_ok = min(use["B1"], use["B2"]) >= min(use["A1"], use["A2"])
    text = {k: [row["reply"] for row in blocks[k]["rows"]] for k in need}
    eq = lambda x, y: sum(a == b for a, b in zip(text[x], text[y]))  # noqa: E731
    paired = [statistics.mean([b1, b2]) - statistics.mean([a1, a2])
              for a1, a2, b1, b2 in zip(fs["A1"], fs["A2"], fs["B1"], fs["B2"])]
    if not quality_ok:
        verdict = "REVERT"
    elif -diff > aa and ordered:
        verdict = "KEEP"
    else:
        verdict = "NO_GAIN"
    print(json.dumps({
        "verdict": verdict, "cpu": r["runner"], "a_p50_s": round(a_p50, 4), "b_p50_s": round(b_p50, 4),
        "diff_s": round(diff, 4), "diff_pct": round(100 * diff / a_p50, 2), "aa_spread_s": round(aa, 4),
        "bb_spread_s": round(bb, 4), "block_p50_s": {k: round(v, 4) for k, v in p.items()},
        "both_b_blocks_faster_than_both_a": ordered,
        "paired_case_diff_median_s": round(statistics.median(paired), 4),
        "paired_cases_b_faster": sum(d < 0 for d in paired), "n_cases": len(paired),
        "usable": use, "quality_gate": quality_ok,
        "text_equal": {"A1_A2": eq("A1", "A2"), "B1_B2": eq("B1", "B2"), "A1_B1": eq("A1", "B1"), "A2_B2": eq("A2", "B2")},
        "ttft_p50_s": {k: blocks[k]["summary"]["ttft_p50_s"] for k in need},
        "prompt_ms_p50": {k: blocks[k]["summary"]["prompt_ms_p50"] for k in need},
        "prompt_n_p50": blocks["A1"]["summary"]["prompt_n_p50"],
        "load_s": {k: blocks[k]["load_s"] for k in need}}, indent=1))


if __name__ == "__main__":
    main()
