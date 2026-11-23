#!/usr/bin/env python3
"""Seed sweep for the barge-in replay harness (bargein_replay/1), Python stdlib only.

For each seed, runs bench/bargein_replay.py twice with --arm-c (gate rho 0.7 and 0.9, M = --min-speech-ms),
200 trials, defaults otherwise. Arms A and B are taken from the rho-0.7 run and checked to be identical
in the rho-0.9 run. Writes one canonical JSON file per seed (no timestamps, no host data), so two runs of
the same harness revision must produce byte-identical files.

  python3 bench/bargein_sweep.py --seeds 1000-1009 --out out/ [--jobs 4]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "bargein_replay.py"
RHOS = (0.7, 0.9)


def parse_seeds(text):
    seeds = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            seeds.extend(range(int(lo), int(hi) + 1))
        else:
            seeds.append(int(part))
    return seeds


def arm_summary(arm):
    valid = [t for t in arm["trials"] if t["kind"] == "valid"]
    out = {k: v for k, v in arm.items() if k != "trials"}
    if any("gate_echo_vetoes" in t["trace"] for t in arm["trials"]):
        out["valid_trials_with_veto"] = sum(t["trace"].get("gate_echo_vetoes", 0) > 0 for t in valid)
    return out


def run_one(seed, rho, m, trials):
    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.json"
        subprocess.run([sys.executable, "-B", str(HARNESS), "--seed", str(seed), "--trials", str(trials),
                        "--arm-c", "--gate-echo-rho", str(rho), "--gate-min-speech-ms", str(m),
                        "--output", str(full)], check=True, stdout=subprocess.DEVNULL)
        data = full.read_bytes()
    return sha256(data).hexdigest(), json.loads(data)


def sweep_seed(seed, m, trials):
    runs = {rho: run_one(seed, rho, m, trials) for rho in RHOS}
    base = runs[RHOS[0]][1]
    ab_same = all(json.dumps(r[1]["arms"][a], sort_keys=True) == json.dumps(base["arms"][a], sort_keys=True)
                  for r in runs.values() for a in ("A", "B"))
    arms = {"A": arm_summary(base["arms"]["A"]), "B": arm_summary(base["arms"]["B"])}
    for rho, (_, res) in runs.items():
        arms["C%s" % rho] = arm_summary(res["arms"]["C"])
    return {
        "harness": "bargein_replay/1",
        "seed": seed,
        "trials_per_arm": trials,
        "gate_min_speech_ms": m,
        "ab_identical_across_rho_runs": ab_same,
        "full_output_sha256": {"rho%s" % rho: runs[rho][0] for rho in RHOS},
        "arms": arms,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds", required=True, help="e.g. 1000-1009 or 1000,1001")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--min-speech-ms", type=int, default=40)
    p.add_argument("--trials", type=int, default=200)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    seeds = parse_seeds(a.seeds)

    def work(seed):
        res = sweep_seed(seed, a.min_speech_ms, a.trials)
        path = a.out / ("seed-%d.json" % seed)
        path.write_bytes((json.dumps(res, sort_keys=True, indent=1) + "\n").encode())
        c = res["arms"]
        print("seed %d A p95 %s fc %d | B p95 %s fc %d | C0.7 p95 %s fc %d miss %d | C0.9 p95 %s fc %d miss %d" % (
            seed, c["A"]["latency_ms"]["p95"], c["A"]["false_barge_ins"], c["B"]["latency_ms"]["p95"],
            c["B"]["false_barge_ins"], c["C0.7"]["latency_ms"]["p95"], c["C0.7"]["false_barge_ins"],
            c["C0.7"]["missed_valid"], c["C0.9"]["latency_ms"]["p95"], c["C0.9"]["false_barge_ins"],
            c["C0.9"]["missed_valid"]), flush=True)
        return res["ab_identical_across_rho_runs"]

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        ok = list(ex.map(work, seeds))
    if not all(ok):
        raise SystemExit("arms A/B differ between rho runs")


if __name__ == "__main__":
    main()
