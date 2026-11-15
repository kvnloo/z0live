"""Benchmark the broker's escalation tiers on the shared in-app control labelled set.

    python3 desktop/tests/bench_phone_tiers.py [--models qwen3-0.6b-q8,qwen3-1.7b-q4km] [--jev] [--out FILE]

Candidates per case are every labelled control on the screen in screen order (what the
phone sends with `status: choose`), so this is harder than real escalations, which arrive
ranked by tier 0. A case counts as acted when confidence clears the tier's floor; an
ambiguous case (expect null) is right only when the tier abstains.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import phone_control as pc  # noqa: E402

FIXTURE = Path(__file__).with_name("fixtures") / "z0live-control-cases.json"


def role(n):
    c = (n.get("cls") or "").lower()
    if n.get("editable") or c.endswith("edittext"):
        return "edit"
    if n.get("checkable") or c.endswith("switch"):
        return "toggle"
    return "button" if n.get("clickable") or c.endswith("button") else "text"


def id_words(view_id):
    import re

    tail = (view_id or "").rsplit("/", 1)[-1]
    return re.sub(r"\s+", " ", re.sub(r"[_.-]", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tail))).strip().lower()


def candidates(screen):
    """Same shape as the phone's choose result, through the broker's own candidates_of."""
    raw = []
    for i, n in enumerate(screen["nodes"]):
        if n.get("label") and (n.get("clickable") or n.get("editable") or n.get("longClickable")):
            raw.append({"id": f"n{i}", "l": n["label"], "r": role(n), "v": id_words(n.get("viewId"))})
    out = pc.candidates_of({"candidates": raw})
    # Settings-style rows: the row is clickable, its label is on the child.
    for i, n in enumerate(screen["nodes"]):
        p = n.get("parent", -1)
        if n.get("label") and p >= 0 and not screen["nodes"][p].get("label") and screen["nodes"][p].get("clickable"):
            if not any(c["id"] == f"n{p}" for c in out):
                out.append({"id": f"n{p}", "label": n["label"], "role": "button"})
    return out[: len(pc.LETTERS)]


def run(tier, floor, data):
    rows = []
    for case in data["cases"]:
        screen = data["screens"][case["screen"]]
        cands = candidates(screen)
        expect = case.get("expect")
        want = next((c["id"] for c in cands if expect and (c["label"] == expect or c["label"].startswith(expect + " ["))), None)
        try:
            d = tier.choose(case["goal"], cands)
        except Exception as exc:  # noqa: BLE001
            d = {"id": None, "confidence": 0.0, "ms": 0.0, "error": type(exc).__name__}
        need = floor if floor is not None else d.get("floor", 0.65)
        acted = bool(d.get("id")) and d.get("confidence", 0) >= need
        right_pick = d.get("id") == want if want else False
        if expect is None:
            verdict = "abstain_ok" if not acted else "acted_on_ambiguous"
        elif acted:
            verdict = "acted_right" if right_pick else "ACTED_WRONG"
        else:
            verdict = "abstained"
        rows.append({"goal": case["goal"], "screen": case["screen"], "expect": expect,
                     "picked": next((c["label"] for c in cands if c["id"] == d.get("id")), None),
                     "confidence": d.get("confidence"), "ms": d.get("ms"), "verdict": verdict,
                     "top1_right": right_pick})
    return rows


def summary(name, rows):
    ms = sorted(r["ms"] for r in rows if r.get("ms"))
    labelled = [r for r in rows if r["expect"] is not None]
    count = lambda v: sum(r["verdict"] == v for r in rows)  # noqa: E731
    return {
        "tier": name, "cases": len(rows),
        "top1_accuracy": round(sum(r["top1_right"] for r in labelled) / len(labelled), 3),
        "acted_right": count("acted_right"), "acted_wrong": count("ACTED_WRONG") + count("acted_on_ambiguous"),
        "abstained": count("abstained") + count("abstain_ok"),
        "p50_ms": round(statistics.median(ms), 1) if ms else None,
        "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1) if ms else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3-0.6b-q8,qwen3-1.7b-q4km,qwen3-4b-q4km")
    ap.add_argument("--jev", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    data = json.loads(FIXTURE.read_text())
    report = {"fixture": str(FIXTURE), "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tiers": [], "rows": {}}
    for model in [m for m in args.models.split(",") if m]:
        tier = pc.Tier1(model=model, timeout=60)
        tier.choose("warm up", [{"id": "a", "label": "OK", "role": "button"}, {"id": "b", "label": "Cancel", "role": "button"}])
        rows = run(tier, pc.TIER1_FLOOR, data)
        report["tiers"].append(summary("groot:" + model, rows))
        report["rows"]["groot:" + model] = rows
        print(json.dumps(report["tiers"][-1]))
    if args.jev:
        rows = run(pc.Tier2(timeout=5.0), None, data)
        report["tiers"].append(summary("jev", rows))
        report["rows"]["jev"] = rows
        print(json.dumps(report["tiers"][-1]))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
