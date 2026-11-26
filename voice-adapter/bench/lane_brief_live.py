"""C56-live-compress: build the arm-B suite with brief_compress.compress_brief (no fixture markers, no model).

  python3 lane_brief_live.py            # writes slm-v2-live.json next to slm-v2.json
  python3 lane_brief_live.py --check    # rebuilds in memory and fails if the committed file differs
  python3 lane_brief_live.py --dev      # offline dev table: per-case lane system-prompt chars, full / e5 / live

Budget = len(e5 brief) = 2810 chars, so the live brief is compared with e5 at the same size. Cases, frozen fields and
every other suite key are copied unchanged; only "brief" (and the suite label / note) differ. Synthetic, no owner data.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import slm_bench  # noqa: E402

BUDGET = 2810  # = len(slm-v2-e5.json brief)


def live_suite(full):
    brief = brief_compress.compress_brief(full["brief"], BUDGET)
    out = dict(full)
    out.update({"suite": "slm-v2-live", "brief": brief,
                "note": "slm-v2 cases unchanged; brief = brief_compress.compress_brief(slm-v2 brief, 2810): header, "
                        "trailer, newest exchange, short-reply (fact) exchanges, then filler spread evenly (every other filler, newest first; C56-live-compress-r3), then a filler newest exchange last (C56-live-compress-r4). "
                        "Synthetic, no owner chat data.",
                "derived": {"from": "bench/slm-v2.json", "mode": "live compress_brief",
                            "budget_chars": BUDGET, "out_chars": len(brief), "builder": "bench/lane_brief_live.py"}})
    return out


def render(full):
    return json.dumps(live_suite(full), indent=1, ensure_ascii=False) + "\n"


def dev(full):
    e5 = json.loads((HERE / "slm-v2-e5.json").read_text())
    live = live_suite(full)
    print(f"brief chars: full {len(full['brief'])} e5 {len(e5['brief'])} live {len(live['brief'])}")
    print("idx case            full    e5  live  live-e5")
    for i, case in enumerate(full["cases"]):
        n = [len(slm_bench.messages(s, case)[0]) for s in (full, e5, live)]
        print(f"{i:>3} {case['id']:<14} {n[0]:>5} {n[1]:>5} {n[2]:>5} {n[2] - n[1]:>+7}")
    kept_e5 = set(e5["brief"].split("\n"))
    kept_live = set(live["brief"].split("\n"))
    print("lines only in e5:", sorted(ln[:50] for ln in kept_e5 - kept_live))
    print("lines only in live:", sorted(ln[:50] for ln in kept_live - kept_e5))


if __name__ == "__main__":
    src, dst = HERE / "slm-v2.json", HERE / "slm-v2-live.json"
    full = json.loads(src.read_text())
    text = render(full)
    if "--dev" in sys.argv[1:]:
        dev(full)
    elif "--check" in sys.argv[1:]:
        assert dst.read_text() == text, "slm-v2-live.json is stale; rebuild with lane_brief_live.py"
        print("ok", dst.name, len(json.loads(text)["brief"]), "chars")
    else:
        dst.write_text(text)
        print(dst, len(json.loads(text)["brief"]), "chars")
