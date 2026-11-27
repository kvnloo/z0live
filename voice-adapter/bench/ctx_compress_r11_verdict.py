"""C1-ctx-compress-r11 frozen verdict (rule in the r11 PREREG; never edited after scoring).

  python ctx_compress_r11_verdict.py job*.json > verdict.json

The rule is the frozen r10 verdict (ctx_compress_r10_verdict.verdict, gates G1-G9, band, NULL checks, presence-split
INVENTED) applied unchanged to the r11 arms: A = r4 (baseline), B = r11 (production), R = r9 (report-only rival,
block R1), O = full brief (report-only, block O1). Presence is ctx_compress_r11_eval.presence() (the frozen r10
presence() over A = r4 and B = r11), so INVENTED is gated over probes whose planted value is in the r4 or the r11
brief; probes absent from both are report-only.

r11 adds one NULL check: every job must carry meta r11_harness == "ctx-compress-r11" (the run used the r11 arms).
Gate G1 (PRIMARY) is r10 cue-word false-merge OLD-fact loss: max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band.
The deterministic pre-check (r11 PREREG) found that no arm fits both facts of a pair at 2810, so the newer fact
(e_new) recall stays report-only and absent-in-both for INVENTED.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ctx_compress_r10_verdict as v10  # noqa: E402

HARNESS = "ctx-compress-r11"
ARMS = {"A": "r4 (bench/brief_compress_r4.py)", "B": "r11 (brief_compress.py)",
        "R": "r9 (bench/brief_compress_r9.py, report-only)", "O": "full brief (report-only)"}


def load_presence():
    import ctx_compress_r11_eval as r11e  # noqa: E402
    return r11e.presence()


def verdict(jobs, pres=None):
    pres = load_presence() if pres is None else pres
    out = v10.verdict(jobs, pres)
    bad = sorted({str(j.get("scenarios")) + "/" + str(j.get("r10_set")) for j in jobs
                  if j.get("r11_harness") != HARNESS})
    reasons = [f"job without r11_harness meta: {b}" for b in bad]
    if not jobs:
        reasons.append("no jobs")
    if reasons:
        out["null_reasons"] = out["null_reasons"] + reasons
        out["outcome"], out["failed"] = "NULL", []
    out["experiment"] = "C1-ctx-compress-r11"
    out["arms"] = ARMS
    return out


if __name__ == "__main__":
    print(json.dumps(verdict([json.load(open(p)) for p in sys.argv[1:]]), indent=1))
