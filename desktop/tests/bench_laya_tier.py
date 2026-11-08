"""Laya (convaiinnovations/laya, 421M ModernBERT decision model) as an escalation tier.

    <python with torch+transformers+laya> desktop/tests/bench_laya_tier.py [--device cuda|cpu] [--out FILE]

Same labelled set, candidates and verdicts as bench_phone_tiers.py. Laya never generates:
one forward pass scores each candidate at its own [MASK] marker and returns calibrated
probabilities, so it cannot invent a target either. Prototype only: the broker does not call it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_phone_tiers as bench  # noqa: E402

LAYA_HF = "convaiinnovations/laya"


class LayaTier:
    def __init__(self, device: str, model: str = LAYA_HF):
        import laya

        started = time.perf_counter()
        self.agent = laya.load(model, device=device)
        self.load_ms = (time.perf_counter() - started) * 1000

    def choose(self, goal: str, cands: list[dict]) -> dict:
        keys = [f"c{i}" for i in range(len(cands))]
        criteria = {}
        for key, c in zip(keys, cands):
            desc = f"{c['label']} ({c.get('role', 'button')})"
            criteria[key] = desc
        state = {"owner_said": goal, "screen": "Controls on the phone screen: " + "; ".join(criteria.values())}
        questions = {"target": {"type": "choice",
                                "instructions": "Which on-screen control should be tapped to do what the owner said?",
                                "criteria": criteria}}
        started = time.perf_counter()
        out = self.agent.predict(state, questions)
        ms = (time.perf_counter() - started) * 1000
        ans = out["answers"]["target"]
        pick = ans.get("choice")
        idx = keys.index(pick) if pick in keys else -1
        return {"id": cands[idx]["id"] if idx >= 0 else None, "confidence": float(ans.get("confidence") or 0.0),
                "ms": round(ms, 1), "tier": "laya"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--model", default=LAYA_HF, help="hub id or local snapshot dir")
    ap.add_argument("--floor", type=float, default=0.85)
    ap.add_argument("--out")
    args = ap.parse_args()
    data = json.loads(bench.FIXTURE.read_text())
    tier = LayaTier(args.device, args.model)
    tier.choose("warm up", [{"id": "a", "label": "OK", "role": "button"}, {"id": "b", "label": "Cancel", "role": "button"}])
    rows = bench.run(tier, args.floor, data)
    s = bench.summary(f"laya:{args.device}", rows)
    s["load_ms"] = round(tier.load_ms)
    s["floor"] = args.floor
    print(json.dumps(s))
    for floor in (0.5, 0.7, 0.85, 0.95):
        acted = [r for r in rows if r["confidence"] is not None and r["confidence"] >= floor and r["picked"]]
        right = sum(r["top1_right"] for r in acted)
        print(json.dumps({"floor": floor, "acted": len(acted), "right": right, "wrong": len(acted) - right}))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": s, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
