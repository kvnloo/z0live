"""Where did each backchannel clip land in a replay run? For every `backchannel.clip` trace event, find the AI
talkspurt that starts at the clip's recorder time (NAME-clock.json maps trace wall time to WAV time) and say whether
dessa_timing scored it as a backchannel (inside the human's talkspurt), a floor-take, or part of a longer AI spurt.
Also reports the clip's correlation with the asset (a garbled or doubled clip correlates poorly).

  python bench/bc_check.py RUN_DIR... [--clip CLIP.wav]
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import dessa_timing as dt  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="+")
    # A10's clip, cut by bc_clips.py from replay a8g1 (GPT-Live's own "Mm-h").
    p.add_argument("--clip", default=str(Path.home() / ".local/share/z0live/bench/dessa/bc-clips"
                                         / "a8g1-gpt-live-dc-comics-41.95.wav"))
    a = p.parse_args()
    ref = dt.pcm(a.clip)
    tally = Counter()
    for run in map(Path, a.runs):
        calls = {}
        for f in run.glob("traces/*/*.jsonl"):
            evs = [json.loads(line) for line in open(f)]
            clips = [e["wall"] for e in evs if e["ev"] == "backchannel.clip"]
            calls[f] = clips
        for clock_path in sorted(run.glob("*-clock.json")):
            name = clock_path.name[: -len("-clock.json")]
            clock = json.loads(clock_path.read_text())
            s0 = clock["sender_wall"]
            clips = [w - s0 for c in calls.values() for w in c if 0 < w - s0 < 90]
            if not clips:
                continue
            h, x = dt.pcm(run / f"{name}-human.wav"), dt.pcm(run / f"{name}-ai.wav")
            hs, xs = dt.spurts(h), dt.spurts(x)
            for t in clips:
                sp = next(((s, e) for s, e in xs if s - 0.35 <= t <= e), None)
                if sp is None:
                    kind = "not-found"
                elif sp[1] - sp[0] > 0.8:
                    kind = "merged"
                elif any(hs0 <= sp[0] and sp[1] <= he for hs0, he in hs):
                    kind = "backchannel"
                else:
                    kind = "floor-take"
                corr = 0.0
                if sp:
                    seg = x[int(sp[0] * 16000) - 1600: int(sp[0] * 16000) + len(ref) + 1600]
                    if len(seg) > len(ref):
                        c = np.correlate(seg, ref, "valid")
                        corr = float(c.max() / (np.linalg.norm(ref) * np.sqrt(np.convolve(seg ** 2, np.ones(len(ref)), "valid").max()) + 1e-9))
                tally[kind] += 1
                print(f"{run.name}/{name} t={t:.2f} {kind} spurt={sp and (round(sp[0], 2), round(sp[1], 2))} corr={corr:.2f}")
    print("TOTAL", dict(tally))


if __name__ == "__main__":
    main()
