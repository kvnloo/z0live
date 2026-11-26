"""C56-brief: build the shorter 'e5' lane brief from the frozen public slm-v2 suite.

  python3 lane_brief_e5.py            # writes slm-v2-e5.json next to slm-v2.json
  python3 lane_brief_e5.py --check    # rebuilds in memory and fails if the committed file differs

e5 (first tested in GH11 on ci/gh11-even-minus-p0, bench/ghci/make_half.py mode "e5") keeps the header, the 6
fact-bearing chat pairs, the Status line and the general-knowledge filler pairs 2, 4, 6, 8, 10 of 12, in original
order. Same rule as GH11; this copy uses the speaker label and fact name of the public (scrubbed) slm-v2 text. Cases, frozen fields and every other suite key are copied unchanged; only "brief" (and the suite label / note)
differ, so a lane request built by slm_bench.messages differs from the full suite only in the brief text.
Synthetic, no owner chat data.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FACT_MARKERS = ("dentist", "Maya's flight", "Tokyo trip", "default voice engine", "Mac Studio", "adb screen access")
SPEAKERS = ("User: ",)
KEEP_FILLER = (2, 4, 6, 8, 10)


def e5_brief(full_brief):
    lines = full_brief.split("\n")
    header, status, turns = lines[0], lines[-1], lines[1:-1]
    assert header.startswith("Recent messages") and status.startswith("Status:"), (header, status)
    keep, n_fact, n_filler = [], 0, 0
    for user, hermes in zip(turns[0::2], turns[1::2]):
        assert user.startswith(SPEAKERS) and hermes.startswith("Hermes: "), (user, hermes)
        if any(m in hermes for m in FACT_MARKERS):
            n_fact += 1
            keep += [user, hermes]
        else:
            if n_filler in KEEP_FILLER:
                keep += [user, hermes]
            n_filler += 1
    assert (n_fact, n_filler) == (6, 12), (n_fact, n_filler)
    return "\n".join([header, *keep, status])


def e5_suite(full):
    brief = e5_brief(full["brief"])
    out = {k: v for k, v in full.items()}
    out.update({"suite": "slm-v2-e5", "brief": brief,
                "note": "slm-v2 cases unchanged; brief = slm-v2 chat brief with filler pairs 2,4,6,8,10 of 12 kept "
                        "(6 fact pairs, header and Status line verbatim). Synthetic, no owner chat data.",
                "derived": {"from": "bench/slm-v2.json", "mode": "e5 (GH11): even filler pairs minus pair 0",
                            "out_chars": len(brief), "builder": "bench/lane_brief_e5.py"}})
    return out


def render(full):
    return json.dumps(e5_suite(full), indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    src, dst = HERE / "slm-v2.json", HERE / "slm-v2-e5.json"
    text = render(json.loads(src.read_text()))
    if "--check" in sys.argv[1:]:
        assert dst.read_text() == text, "slm-v2-e5.json is stale; rebuild with lane_brief_e5.py"
        print("ok", dst.name, len(json.loads(text)["brief"]), "chars")
    else:
        dst.write_text(text)
        print(dst, len(json.loads(text)["brief"]), "chars")
