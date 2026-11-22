"""Build the 'half' slm-v2 brief: the full chat brief with every other general-knowledge filler pair dropped.

  python3 make_half.py   # writes suites/slm-v2-half.json from ../slm-v2.json

GH8 showed the medium brief (header + 6 fact pairs + Status, no filler) loses must-SKIP items the full brief
holds, so the 12 filler pairs (length / turn count) carry SKIP discipline. This is the dose step: keep all 6
fact-bearing pairs, the header and Status line, and filler pairs 0, 2, 4, ... (6 of 12) in original order.
GH10: parity=1 keeps the complementary odd-indexed filler pairs 1, 3, ..., 11 (suites/slm-v2-odd.json) at about the
same length, to split filler length from pair content.
GH11: "e5" keeps the even half minus filler pair 0 (pairs 2,4,..,10; suites/slm-v2-e5.json) to test whether the
first filler position carries the even half's advantage over the odd half.
GH12: "es4" is the even half with filler pair 4 (sci-fi, 313 chars) replaced in place by odd pair 1 (dinner, 333,
the closest-length odd pair). Length and positions stay fixed, and only one pair's content changes.
GH13: "es2" is the even half with filler pair 2 (heat pump, 499 chars, the slot before the Harsha fact) replaced in
place by odd pair 5 (mutex, 475, the closest-length odd pair, -24 chars).
GH14: "es3" is the even half with filler pair 2 replaced in place by odd pair 3 (houseplants, 352, -147 chars): is
GH13's u-wait loss a pre-Harsha slot effect or pair-5 content?
Deterministic; fact pairs identified by make_medium.FACT_MARKERS.

  python3 make_half.py odd   # writes suites/slm-v2-odd.json
  python3 make_half.py e5    # writes suites/slm-v2-e5.json
  python3 make_half.py es4   # writes suites/slm-v2-es4.json
  python3 make_half.py es2   # writes suites/slm-v2-es2.json
  python3 make_half.py es3   # writes suites/slm-v2-es3.json
"""
import json
import sys
from pathlib import Path

from make_medium import FACT_MARKERS

HERE = Path(__file__).resolve().parent


def half_brief(full_brief, parity=0, drop=(), swap=None):
    """swap = {kept filler index: replacement filler index}; the replacement pair takes the kept pair's slot."""
    lines = full_brief.split("\n")
    header, status, turns = lines[0], lines[-1], lines[1:-1]
    assert header.startswith("Recent messages") and status.startswith("Status:"), (header, status)
    pairs = list(zip(turns[0::2], turns[1::2]))
    fillers = [p for p in pairs if not any(m in p[1] for m in FACT_MARKERS)]
    keep, n_fact, n_filler, n_kept_filler = [], 0, 0, 0
    for kevin, hermes in pairs:
        assert kevin.startswith("Kevin: ") and hermes.startswith("Hermes: "), (kevin, hermes)
        if any(m in hermes for m in FACT_MARKERS):
            n_fact += 1
            keep += [kevin, hermes]
        else:
            if n_filler % 2 == parity and n_filler not in drop:
                n_kept_filler += 1
                keep += list(fillers[swap[n_filler]]) if swap and n_filler in swap else [kevin, hermes]
            n_filler += 1
    assert (n_fact, n_filler, n_kept_filler) == (6, 12, 6 - len(drop)), (n_fact, n_filler, n_kept_filler)
    return "\n".join([header, *keep, status])


SUITE_NAME = {0: "slm-v2-half", 1: "slm-v2-odd", "e5": "slm-v2-e5", "es4": "slm-v2-es4", "es2": "slm-v2-es2",
              "es3": "slm-v2-es3"}
KEPT = {0: "0,2,4,6,8,10", 1: "1,3,5,7,9,11", "e5": "2,4,6,8,10", "es4": "0,2,1,6,8,10 (pair 1 in pair 4's slot)",
        "es2": "0,5,4,6,8,10 (pair 5 in pair 2's slot)", "es3": "0,3,4,6,8,10 (pair 3 in pair 2's slot)"}
# key -> (parity, dropped filler indices[, swap {kept index: replacement index}])
VARIANT = {0: (0, ()), 1: (1, ()), "e5": (0, (0,)), "es4": (0, (), {4: 1}), "es2": (0, (), {2: 5}),
           "es3": (0, (), {2: 3})}
MODE = {0: "", 1: ", odd-indexed", "e5": ", even-indexed minus pair 0",
        "es4": ", even-indexed with pair 4 replaced in place by pair 1 (closest odd length, +20 chars)",
        "es2": ", even-indexed with pair 2 replaced in place by pair 5 (closest odd length, -24 chars)",
        "es3": ", even-indexed with pair 2 replaced in place by pair 3 (-147 chars)"}


def variant_brief(full_brief, key=0):
    return half_brief(full_brief, *VARIANT[key])


def half_suite(full, parity=0):
    brief = variant_brief(full["brief"], parity)
    return {"suite": SUITE_NAME[parity], "frozen": full.get("frozen"),
            "note": f"slm-v2 cases unchanged; brief = slm-v2 chat brief with filler pairs {KEPT[parity]} of 12 kept "
                    "(6 fact pairs, header and Status line verbatim). Synthetic, no owner chat data.",
            "brief": brief, "cases": full["cases"],
            "derived": {"from": "bench/slm-v2.json",
                        "mode": "every other filler pair" + MODE[parity],
                        "out_chars": len(brief), "builder": "bench/ghci/make_half.py"}}


if __name__ == "__main__":
    full = json.loads((HERE.parent / "slm-v2.json").read_text())
    parity = {"odd": 1, "e5": "e5", "es4": "es4", "es2": "es2", "es3": "es3"}.get(sys.argv[1] if sys.argv[1:] else "", 0)
    out = HERE / f"suites/{SUITE_NAME[parity]}.json"
    out.write_text(json.dumps(half_suite(full, parity), indent=1, ensure_ascii=False) + "\n")
    print(out, len(variant_brief(full["brief"], parity)), "chars")
