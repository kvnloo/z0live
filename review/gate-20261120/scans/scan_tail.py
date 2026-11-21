"""Timings only, by adapter rev: does phone-bound audio keep playing after the provider's assistant turn.done?

For each primary assistant turn.done: was output audio on at that moment, and how long until the next output
audio.off. guest-floor (a70a58a) silences primary frames once the reply is popped at turn.done when a GPT guest is
on the call; if the base shows audio regularly running past turn.done, the new gate clips reply tails.
usage: python3 -I scan_tail.py <trace root>
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main(root):
    tails = defaultdict(list)
    for path in sorted(Path(root).glob("**/*.jsonl")):
        rev, guest, on, waiting = None, False, False, []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ev, t = e.get("ev"), e.get("t", 0)
                if ev == "call.created":
                    rev = e.get("rev")
                elif ev == "guest.ready":
                    guest = True
                elif ev == "audio.on" and e.get("dir") == "output":
                    on = True
                elif ev == "audio.off" and e.get("dir") == "output":
                    on = False
                    for done in waiting:
                        tails[(rev, guest)].append(t - done)
                    waiting = []
                elif (ev == "rx" and e.get("src") == "provider" and e.get("type") == "turn.done"
                      and ((e.get("event") or {}).get("turn") or {}).get("role") == "assistant" and on):
                    waiting.append(t)
    for (rev, guest), xs in sorted(tails.items(), key=lambda kv: str(kv[0])):
        xs.sort()
        p90 = xs[min(len(xs) - 1, int(0.9 * len(xs)))]
        over = sum(x > 1.0 for x in xs)
        print(f"rev={rev} gpt_guest={guest} n={len(xs)} audio_on_at_turn.done -> audio.off: "
              f"p50={statistics.median(xs):.2f}s p90={p90:.2f}s max={xs[-1]:.2f}s over_1s={over}")


if __name__ == "__main__":
    main(sys.argv[1])
