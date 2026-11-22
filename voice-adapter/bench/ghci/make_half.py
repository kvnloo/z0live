"""Build the 'half' slm-v2 brief: the full chat brief with every other general-knowledge filler pair dropped.

  python3 make_half.py   # writes suites/slm-v2-half.json from ../slm-v2.json

GH8 showed the medium brief (header + 6 fact pairs + Status, no filler) loses must-SKIP items the full brief
holds, so the 12 filler pairs (length / turn count) carry SKIP discipline. This is the dose step: keep all 6
fact-bearing pairs, the header and Status line, and filler pairs 0, 2, 4, ... (6 of 12) in original order.
GH10: parity=1 keeps the complementary odd-indexed filler pairs 1, 3, ..., 11 (suites/slm-v2-odd.json) at about the
same length, to split filler length from pair content.
Deterministic; fact pairs identified by make_medium.FACT_MARKERS.

  python3 make_half.py odd   # writes suites/slm-v2-odd.json
"""
import json
import sys
from pathlib import Path

from make_medium import FACT_MARKERS

HERE = Path(__file__).resolve().parent


def half_brief(full_brief, parity=0):
    lines = full_brief.split("\n")
    header, status, turns = lines[0], lines[-1], lines[1:-1]
    assert header.startswith("Recent messages") and status.startswith("Status:"), (header, status)
    keep, n_fact, n_filler, n_kept_filler = [], 0, 0, 0
    for kevin, hermes in zip(turns[0::2], turns[1::2]):
        assert kevin.startswith("Kevin: ") and hermes.startswith("Hermes: "), (kevin, hermes)
        if any(m in hermes for m in FACT_MARKERS):
            n_fact += 1
            keep += [kevin, hermes]
        else:
            if n_filler % 2 == parity:
                n_kept_filler += 1
                keep += [kevin, hermes]
            n_filler += 1
    assert (n_fact, n_filler, n_kept_filler) == (6, 12, 6), (n_fact, n_filler, n_kept_filler)
    return "\n".join([header, *keep, status])


SUITE_NAME = {0: "slm-v2-half", 1: "slm-v2-odd"}
KEPT = {0: "0,2,4,6,8,10", 1: "1,3,5,7,9,11"}


def half_suite(full, parity=0):
    brief = half_brief(full["brief"], parity)
    return {"suite": SUITE_NAME[parity], "frozen": full.get("frozen"),
            "note": f"slm-v2 cases unchanged; brief = slm-v2 chat brief with filler pairs {KEPT[parity]} of 12 kept "
                    "(6 fact pairs, header and Status line verbatim). Synthetic, no owner chat data.",
            "brief": brief, "cases": full["cases"],
            "derived": {"from": "bench/slm-v2.json",
                        "mode": "every other filler pair" + (", odd-indexed" if parity else ""),
                        "out_chars": len(brief), "builder": "bench/ghci/make_half.py"}}


if __name__ == "__main__":
    full = json.loads((HERE.parent / "slm-v2.json").read_text())
    parity = 1 if sys.argv[1:] == ["odd"] else 0
    out = HERE / f"suites/{SUITE_NAME[parity]}.json"
    out.write_text(json.dumps(half_suite(full, parity), indent=1, ensure_ascii=False) + "\n")
    print(out, len(half_brief(full["brief"], parity)), "chars")
