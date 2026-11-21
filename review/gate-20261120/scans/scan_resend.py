"""Counts and timings only: each floor event, with the running count of primary spoken-word events.

usage: python3 -I scan_resend.py <trace.jsonl> [from_s] [to_s]
No transcript text is read or printed; `out` counts provider output_transcript.added events.
"""
import json
import sys


def role(e):
    return ((e.get("event") or {}).get("turn") or {}).get("role")


def main(path, lo=0.0, hi=1e9):
    evs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
    t0 = evs[0].get("t", 0)
    ids, out = {}, 0
    for e in evs:
        ev, t = e.get("ev"), e.get("t", 0) - t0
        label = None
        if ev == "rx" and e.get("src") == "provider" and e.get("type") == "output_transcript.added":
            out += 1
        elif ev in ("floor.answer_sent", "floor.answer_queued", "floor.barge_in", "floor.changed",
                    "delegation.start", "delegation.done", "guest.failed"):
            extra = {k: e.get(k) for k in ("speaker", "reason") if k in e}
            if "did" in e:
                extra["did"] = ids.setdefault(e["did"], f"d{len(ids)}")
            label = f"{ev} {extra}"
        elif ev == "rx" and e.get("src") == "provider" and e.get("type") in ("turn.created", "turn.done") \
                and role(e) == "assistant":
            label = f"  primary assistant {e.get('type')}"
        if label and lo <= t <= hi:
            print(f"{t:8.2f}s out={out:4d} {label}")
    print(f"events={len(evs)} span={evs[-1].get('t', 0) - t0:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1], *map(float, sys.argv[2:4]))
