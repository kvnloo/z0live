#!/usr/bin/env python3
"""Seed sweep for the barge-in replay harness (bargein_replay/1..3), Python stdlib only.

For each seed, runs bench/bargein_replay.py twice with --arm-c (gate rho 0.7 and 0.9, M = --min-speech-ms),
200 trials, defaults otherwise (plus optional --echo-model / --bot-render pass-through). Arms A and B are
taken from the rho-0.7 run and checked to be identical in the rho-0.9 run. Writes one canonical JSON file
per seed (no timestamps, no host data), so two runs of the same harness revision must produce byte-identical
files.

  python3 bench/bargein_sweep.py --seeds 1000-1009 --out out/ [--jobs 4]
  python3 bench/bargein_sweep.py --seeds 42,2000-2003 --out out/ --echo-model residual --bot-render recorded
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
    if any("energy_vetoes" in t["trace"] for t in arm["trials"]):  # arm D (bargein_replay/4)
        out["valid_trials_with_veto"] = sum(t["trace"].get("energy_vetoes", 0) > 0 for t in valid)
    if any("erl_vetoes" in t["trace"] for t in arm["trials"]):  # arm E (bargein_replay/6)
        out["valid_trials_with_veto"] = sum(t["trace"].get("erl_vetoes", 0) > 0 for t in valid)
    if any("erl2_vetoes" in t["trace"] for t in arm["trials"]):  # arm E2 (bargein_replay/8)
        out["valid_trials_with_veto"] = sum(t["trace"].get("erl2_vetoes", 0) > 0 for t in valid)
    if any("nlms_vetoes" in t["trace"] for t in arm["trials"]):  # arms F / F2 (bargein_replay/9)
        out["valid_trials_with_veto"] = sum(t["trace"].get("nlms_vetoes", 0) > 0 for t in valid)
    return out


def run_one(seed, rho, m, trials, extra):
    # rho None (--rhos none, A-bargein-M): one run without arm C, so the costly correlation gate is skipped
    arm_c = [] if rho is None else ["--arm-c", "--gate-echo-rho", str(rho), "--gate-min-speech-ms", str(m)]
    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.json"
        subprocess.run([sys.executable, "-B", str(HARNESS), "--seed", str(seed), "--trials", str(trials),
                        *arm_c, *extra, "--output", str(full)], check=True, stdout=subprocess.DEVNULL)
        data = full.read_bytes()
    return sha256(data).hexdigest(), json.loads(data)


def sweep_seed(seed, m, trials, extra, rhos=RHOS):
    runs = {rho: run_one(seed, rho, m, trials, extra) for rho in rhos}
    base = runs[rhos[0]][1]
    shared = ("A", "B") + tuple(a for a in ("D", "E", "E2", "F", "F2", "G", "G2", "H", "O")
                                if a in base["arms"])  # D, E (opt-in) do not depend on rho
    ab_same = all(json.dumps(r[1]["arms"][a], sort_keys=True) == json.dumps(base["arms"][a], sort_keys=True)
                  for r in runs.values() for a in shared)
    arms = {a: arm_summary(base["arms"][a]) for a in shared}
    for rho, (_, res) in runs.items():
        if rho is not None:
            arms["C%s" % rho] = arm_summary(res["arms"]["C"])
    result = {
        "harness": base.get("harness", "bargein_replay/1"),
        "seed": seed,
        "trials_per_arm": trials,
        "gate_min_speech_ms": m,
        "ab_identical_across_rho_runs": ab_same,
        "full_output_sha256": {"no-arm-c" if rho is None else "rho%s" % rho: runs[rho][0] for rho in rhos},
        "arms": arms,
    }
    if extra:
        result["extra_args"] = extra
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds", required=True, help="e.g. 1000-1009 or 42,1000,1001")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--min-speech-ms", type=int, default=40)
    p.add_argument("--trials", type=int, default=200)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    p.add_argument("--echo-model", choices=("copy", "residual"), default="copy")
    p.add_argument("--echo-continuous", action="store_true", help="bargein_replay/7 pass-through (residual only)")
    p.add_argument("--echo-erle-db", help="bargein_replay/8 pass-through (needs --echo-continuous), e.g. 20")
    p.add_argument("--arm-e2", action="store_true", help="also run arm E2 (least-squares ERL fit, bargein_replay/8)")
    p.add_argument("--erl2-margin-db", type=float, default=6.0)
    p.add_argument("--erl2-prior-db", type=float, default=-12.0)
    p.add_argument("--erl2-min-updates", type=int, default=10)
    p.add_argument("--erl2-min-speech-ms", type=int, default=30)
    p.add_argument("--arm-f", action="store_true", help="also run arm F (NLMS canceller + D gate, bargein_replay/9)")
    p.add_argument("--arm-f2", action="store_true", help="also run arm F2 (NLMS canceller, echo-estimate veto)")
    p.add_argument("--nlms-mu", type=float, default=0.5)
    p.add_argument("--nlms-delta", type=float, default=1000.0)
    p.add_argument("--nlms-pre-ms", type=int, default=15)
    p.add_argument("--nlms-post-ms", type=int, default=65)
    p.add_argument("--nlms-geigel", type=float, default=0.5)
    p.add_argument("--nlms-hang-ms", type=int, default=30)
    p.add_argument("--nlms-min-updates", type=int, default=10)
    p.add_argument("--nlms-margin-db", type=float, default=6.0)
    p.add_argument("--nlms-converged-db", type=float, default=6.0)
    p.add_argument("--arm-g", action="store_true", help="also run arm G (NLMS + residual-only gate, bargein_replay/10)")
    p.add_argument("--arm-g2", action="store_true", help="also run arm G2 (arm G + residual echo suppressor)")
    p.add_argument("--resid-threshold", type=float, default=480.0)
    p.add_argument("--resid-arm-db", type=float, default=6.0)
    p.add_argument("--resid-nlp-db", type=float, default=3.0)
    p.add_argument("--arm-h", action="store_true", help="also run arm H (two-path NLMS + residual gate, bargein_replay/11)")
    p.add_argument("--twopath-copy-db", type=float, default=1.0)
    p.add_argument("--twopath-copy-packets", type=int, default=2)
    p.add_argument("--arm-o", action="store_true", help="also run arm O (G + shadow-filter guard, bargein_replay/12)")
    p.add_argument("--guard-erle-db", type=float, default=6.0)
    p.add_argument("--guard-margin-db", type=float, default=3.0)
    p.add_argument("--guard-packets", type=int, default=10)
    p.add_argument("--bot-render", choices=("periodic", "recorded"), default="periodic")
    p.add_argument("--rhos", default=",".join(map(str, RHOS)), help="arm C rho values, e.g. 0.9 (default 0.7,0.9)")
    p.add_argument("--arm-d", action="store_true", help="also run arm D (energy-ratio gate, bargein_replay/4)")
    p.add_argument("--energy-k-db", type=float, default=-6.0)
    p.add_argument("--energy-min-speech-ms", type=int, default=40)
    p.add_argument("--arm-e", action="store_true", help="also run arm E (ERL-adaptive energy gate, bargein_replay/6)")
    p.add_argument("--erl-estimator", choices=("quiet", "vetoed"), default="quiet")
    p.add_argument("--erl-alpha", type=float, default=.1)
    p.add_argument("--erl-prior-db", type=float, default=-12.0)
    p.add_argument("--erl-margin-db", type=float, default=6.0)
    p.add_argument("--erl-min-speech-ms", type=int, default=30)
    p.add_argument("--caller-level-db-range", help="bargein_replay/5 stress pass-through, e.g. -10,0")
    p.add_argument("--erl-db-range", help="bargein_replay/5 stress pass-through, e.g. 0,6")
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    seeds = parse_seeds(a.seeds)
    rhos = (None,) if a.rhos == "none" else tuple(float(r) for r in a.rhos.split(","))
    extra = []  # defaults add no flags, so GH16-style bargein_replay/1 sweeps keep their exact bytes
    if a.echo_model != "copy":
        extra += ["--echo-model", a.echo_model]
    if a.echo_continuous:
        extra += ["--echo-continuous"]
    if a.echo_erle_db is not None:
        extra += ["--echo-erle-db", a.echo_erle_db]
    if a.bot_render != "periodic":
        extra += ["--bot-render", a.bot_render]
    if a.arm_d:
        extra += ["--arm-d", "--energy-k-db", str(a.energy_k_db), "--energy-min-speech-ms", str(a.energy_min_speech_ms)]
    if a.arm_e:
        extra += ["--arm-e", "--erl-estimator", a.erl_estimator, "--erl-alpha", str(a.erl_alpha),
                  "--erl-prior-db=" + str(a.erl_prior_db), "--erl-margin-db=" + str(a.erl_margin_db),
                  "--erl-min-speech-ms", str(a.erl_min_speech_ms)]
    if a.arm_e2:
        extra += ["--arm-e2", "--erl2-margin-db=" + str(a.erl2_margin_db), "--erl2-prior-db=" + str(a.erl2_prior_db),
                  "--erl2-min-updates", str(a.erl2_min_updates), "--erl2-min-speech-ms", str(a.erl2_min_speech_ms)]
    if a.arm_f or a.arm_f2 or a.arm_g or a.arm_g2 or a.arm_h or a.arm_o:
        if not a.arm_d:
            extra += ["--energy-k-db=" + str(a.energy_k_db), "--energy-min-speech-ms", str(a.energy_min_speech_ms)]
        extra += (["--arm-f"] if a.arm_f else []) + (["--arm-f2"] if a.arm_f2 else [])
        extra += ["--nlms-mu", str(a.nlms_mu), "--nlms-delta", str(a.nlms_delta), "--nlms-pre-ms", str(a.nlms_pre_ms),
                  "--nlms-post-ms", str(a.nlms_post_ms), "--nlms-geigel", str(a.nlms_geigel),
                  "--nlms-hang-ms", str(a.nlms_hang_ms), "--nlms-min-updates", str(a.nlms_min_updates),
                  "--nlms-margin-db=" + str(a.nlms_margin_db), "--nlms-converged-db=" + str(a.nlms_converged_db)]
    if a.arm_g or a.arm_g2 or a.arm_h or a.arm_o:
        extra += (["--arm-g"] if a.arm_g else []) + (["--arm-g2"] if a.arm_g2 else [])
        extra += ["--resid-threshold", str(a.resid_threshold), "--resid-arm-db=" + str(a.resid_arm_db),
                  "--resid-nlp-db=" + str(a.resid_nlp_db)]
    if a.arm_h:  # bargein_replay/11 (A-bargein-N)
        extra += ["--arm-h", "--twopath-copy-db=" + str(a.twopath_copy_db),
                  "--twopath-copy-packets", str(a.twopath_copy_packets)]
    if a.arm_o:  # bargein_replay/12 (A-bargein-O)
        extra += ["--arm-o", "--guard-erle-db=" + str(a.guard_erle_db), "--guard-margin-db=" + str(a.guard_margin_db),
                  "--guard-packets", str(a.guard_packets)]
    if a.caller_level_db_range:
        extra += ["--caller-level-db-range=" + a.caller_level_db_range]  # "=": the value starts with "-"
    if a.erl_db_range:
        extra += ["--erl-db-range=" + a.erl_db_range]

    def work(seed):
        res = sweep_seed(seed, a.min_speech_ms, a.trials, extra, rhos)
        path = a.out / ("seed-%d.json" % seed)
        path.write_bytes((json.dumps(res, sort_keys=True, indent=1) + "\n").encode())
        print("seed %d " % seed + " | ".join("%s p95 %s fc %d miss %d" % (
            name, arm["latency_ms"]["p95"], arm["false_barge_ins"], arm["missed_valid"])
            for name, arm in sorted(res["arms"].items())), flush=True)
        return res["ab_identical_across_rho_runs"]

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        ok = list(ex.map(work, seeds))
    if not all(ok):
        raise SystemExit("arms A/B differ between rho runs")


if __name__ == "__main__":
    main()
