# C56-lanespeed AMEND-1 (infra fix, the one allowed rerun; the original PREREG is unchanged)

Cause (measured, run 45757375551): cold prefill of the ~1490-token slm-v2 prompt takes ~27 s per request on the
4-vCPU Neoverse-N2 runner, so 4 blocks x 31 requests (~57 min) cannot fit the 45 min job cap. The in-script deadline
stopped A2 at 8/30 cases and B2 never ran; the frozen rule gives NULL for that run.

Disclosure: this amendment is written AFTER run 1 produced partial data (A1 30/30, B1 30/30, A2 8/30). Run 1 is
reported as descriptive only and is not pooled with run 2. The decision rule, knob, model, server flags, metric and
block order are unchanged.

Changed lines (each caused only by the time cap):
1. Cases per block: all 30 -> the 15 cases with even suite index (`lanespeed.py --every 2`): 3 fact, 3 reasoning,
   3 context, 2 followup, 4 must-SKIP. Arm p50 pools 30 values (2 blocks x 15) instead of 60.
2. Quality gate reads the usable count of those 15 cases per block (same rule: each B block >= the lower A block).
3. Expected wall ~30 min for 4 blocks; deadline 1980 s and job timeout 45 min unchanged.
If run 2 is also incomplete, the verdict is NULL (no further rerun).
