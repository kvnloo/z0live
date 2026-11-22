"""Build the 'half' slm-v2 brief: the full chat brief with every other general-knowledge filler pair dropped.

  python3 make_half.py   # writes suites/slm-v2-half.json from ../slm-v2.json

GH8 showed the medium brief (header + 6 fact pairs + Status, no filler) loses must-SKIP items the full brief
holds, so the 12 filler pairs (length / turn count) carry SKIP discipline. This is the dose step: keep all 6
fact-bearing pairs, the header and Status line, and filler pairs 0, 2, 4, ... (6 of 12) in original order.
Deterministic; fact pairs identified by make_medium.FACT_MARKERS.
"""
import json
from pathlib import Path

from make_medium import FACT_MARKERS

HERE = Path(__file__).resolve().parent


def half_brief(full_brief):
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
            if n_filler % 2 == 0:
                n_kept_filler += 1
                keep += [kevin, hermes]
            n_filler += 1
    assert (n_fact, n_filler, n_kept_filler) == (6, 12, 6), (n_fact, n_filler, n_kept_filler)
    return "\n".join([header, *keep, status])


def half_suite(full):
    brief = half_brief(full["brief"])
    return {"suite": "slm-v2-half", "frozen": full.get("frozen"),
            "note": "slm-v2 cases unchanged; brief = slm-v2 chat brief with filler pairs 0,2,4,6,8,10 of 12 kept "
                    "(6 fact pairs, header and Status line verbatim). Synthetic, no owner chat data.",
            "brief": brief, "cases": full["cases"],
            "derived": {"from": "bench/slm-v2.json", "mode": "every other filler pair", "out_chars": len(brief),
                        "builder": "bench/ghci/make_half.py"}}


if __name__ == "__main__":
    full = json.loads((HERE.parent / "slm-v2.json").read_text())
    out = HERE / "suites/slm-v2-half.json"
    out.write_text(json.dumps(half_suite(full), indent=1, ensure_ascii=False) + "\n")
    print(out, len(half_brief(full["brief"])), "chars")
