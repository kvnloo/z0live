"""Field presence only: do provider user-turn events carry the ids and word timestamps GPTGuest.observe needs?

usage: python3 -I scan_event_shapes.py <trace.jsonl>
Counts keys on rx provider events; prints no values.
"""
import json
import sys
from collections import Counter


def main(path):
    seen = Counter()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ev") not in ("rx", "guest.rx") or (e.get("ev") == "rx" and e.get("src") != "provider"):
                continue
            event = e.get("event") or {}
            kind = event.get("type")
            leg = "guest" if e.get("ev") == "guest.rx" else "primary"
            if kind == "turn.delta":
                seen[(leg, kind)] += 1
                for key in ("turn_id", "start_ms", "end_ms", "delta"):
                    seen[(leg, kind, key)] += key in event
            elif kind in ("turn.created", "turn.done") and (event.get("turn") or {}).get("role") == "user":
                seen[(leg, kind + ":user")] += 1
                for key in ("id", "start_ms", "end_ms"):
                    seen[(leg, kind + ":user", key)] += key in event["turn"]
    for key in sorted(seen, key=str):
        print(" ".join(key), seen[key])


if __name__ == "__main__":
    main(sys.argv[1])
