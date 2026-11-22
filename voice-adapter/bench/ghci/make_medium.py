"""Build the 'medium' slm-v2 brief: chat header + the fact-bearing Kevin/Hermes turn pairs verbatim + Status line.

  python3 make_medium.py   # writes suites/slm-v2-chat1k.json from ../slm-v2.json

Keeps the chat-transcript framing the compressed (model1100 key: value) brief drops, and removes the 12
general-knowledge filler pairs. Deterministic: a pair is kept iff its Hermes reply carries one of FACT_MARKERS.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FACT_MARKERS = ("dentist", "Harsha's flight", "Tokyo trip", "default voice engine", "Mac Studio", "adb screen access")


def medium_brief(full_brief):
    lines = full_brief.split("\n")
    header, status, turns = lines[0], lines[-1], lines[1:-1]
    assert header.startswith("Recent messages") and status.startswith("Status:"), (header, status)
    keep = []
    for kevin, hermes in zip(turns[0::2], turns[1::2]):
        assert kevin.startswith("Kevin: ") and hermes.startswith("Hermes: "), (kevin, hermes)
        if any(m in hermes for m in FACT_MARKERS):
            keep += [kevin, hermes]
    assert len(keep) == 2 * len(FACT_MARKERS), len(keep)
    return "\n".join([header, *keep, status])


def medium_suite(full):
    brief = medium_brief(full["brief"])
    return {"suite": "slm-v2-chat1k", "frozen": full.get("frozen"),
            "note": "slm-v2 cases unchanged; brief = slm-v2 chat header + the 6 fact-bearing Kevin/Hermes turn pairs "
                    "verbatim + Status line (12 filler pairs dropped). Synthetic, no owner chat data.",
            "brief": brief, "cases": full["cases"],
            "derived": {"from": "bench/slm-v2.json", "mode": "chat-turn subset", "out_chars": len(brief),
                        "builder": "bench/ghci/make_medium.py"}}


if __name__ == "__main__":
    full = json.loads((HERE.parent / "slm-v2.json").read_text())
    out = HERE / "suites/slm-v2-chat1k.json"
    out.write_text(json.dumps(medium_suite(full), indent=1, ensure_ascii=False) + "\n")
    print(out, len(medium_brief(full["brief"])), "chars")
