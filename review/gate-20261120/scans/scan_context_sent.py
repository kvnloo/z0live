"""Sizes only: bytes of verbatim text in context.sent events, per call and per channel. No text is printed.

usage: python3 -I scan_context_sent.py <trace root>
"""
import json
import sys
from collections import Counter
from pathlib import Path


def main(root):
    for path in sorted(Path(root).glob("**/*.jsonl")):
        sizes, n, turn0 = Counter(), 0, 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"context.sent"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ev") != "context.sent":
                    continue
                n += 1
                size = sum(len(t.encode()) for t in e.get("texts") or [])
                sizes[e.get("channel") or e.get("type")] += size
                if e.get("turn_id") == 0:
                    turn0 += size
        if n:
            print(f"{path.parent.name}/{path.name[:6]} context.sent={n} verbatim_bytes={dict(sizes)} setup_turn0_bytes={turn0}")


if __name__ == "__main__":
    main(sys.argv[1])
