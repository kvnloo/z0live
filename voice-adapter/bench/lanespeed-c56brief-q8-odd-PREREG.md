# C56-brief-q8-odd prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU; Neoverse-N2 in every earlier arm run, including C56, C56-brief, C56-brief-odd and C56-brief-q8). A CPU result
here verdicts nothing about phones, Metal or GPUs.

## Why this run
C56-brief-q8 (run 45789629900, KEEP) showed, at Q8_0 on the 15 even-index slm-v2 cases, that the e5 brief halves cold
first-answer latency (p50 25.266 -> 12.985 s) with usable 14/14/14/14. The odd half at Q8_0 is unmeasured. In GH9,
s-mom (an odd-index must-SKIP case) was an e5-vs-full Q8_0 miss, so the usable gate is the risk on this half. At Q4_K_M
the odd half (C56-brief-odd, run 45773497094, KEEP) had s-mom and f-everest unusable in both arms.

## Hypothesis
H_C56b-q8-odd: on the 15 odd-index slm-v2 cases, with gemma-4-E2B-it Q8_0 in both arms and everything else as in
C56-brief-q8, B (e5 brief) has a first-answer sentence p50 lower than A (full brief) by more than the A/A spread, both B
block p50s are below both A block p50s, and usable(each B block) >= the lower usable of the two A blocks.
Prior (I, not a decision input): latency should halve as on the even half (prefill-bound, prompt_n -41 %); the gate is
at risk on s-mom (GH9 Q8_0 miss; one greedy sample per cell, different harness).

## The one change vs C56-brief-q8
Case selection only: `lanespeed.py --every 2 --offset 1` scores suite indices 1, 3, ..., 29 (f-everest, f-planet,
f-gold, r-mult, r-train, r-days, c-maya, c-engine, c-ram, u-australia, u-correct, s-text, s-files, s-mom, s-weather),
exactly as C56-brief-odd did it (same flag, same dry check lanespeed_c56brief_odd_check.py, unchanged). The unscored
warm-up stays case 0 (f-capital) with the arm's brief. Model (Q8_0 605d3c26), arms, build, server flags, sampling,
scorer and verdict script are byte-identical to C56-brief-q8 (lanespeed_verdict.py 978c699e, as in C56, C56-brief,
C56-brief-odd, C56-brief-q8). Workflow lanespeed-c56brief-q8-odd.yml is a copy of lanespeed-c56brief-q8.yml with only
the selection flag, names, prereg path and trigger branch/path changed. Trigger: the push that adds this workflow file
on branch ci/lanespeed-c56-brief-q8-odd (dispatch by file name 404s off the default branch).

## Time budget (lesson from C56 AMEND-1: the 45 min job cap)
The odd cases have the same mean prompt length as the even cases (C56-brief-odd prereg, measured offline), and the Q4
odd/even walls matched (1352.4 vs 1353.9 s). Predicted bench wall = the C56-brief-q8 measured wall, about 1250 s (I).
In-script deadline 1980 s (about 730 s margin), bench step 36 min, job cap 45 min, download step 15 min. Unchanged.

## Arms and frozen inputs (sha256 in lanespeed-c56brief-q8-odd-FREEZE.sha256)
- A: voice-adapter/bench/slm-v2.json 7fa9f7c6 (full brief, 5540 chars). B: voice-adapter/bench/slm-v2-e5.json
  add17576 (e5 brief, 2810 chars, GH11 rule; cases byte-identical to A).
- Model: unsloth/gemma-4-E2B-it-GGUF rev 0314792d, gemma-4-E2B-it-Q8_0.gguf sha256 605d3c26 (checked on the runner).
- llama.cpp b12527 ubuntu-arm64 prebuilt, tarball sha256 c2758c87.
- slm_bench.py 805a2518, tiers.py 8496ab2c, lanespeed.py 02bd44d6, lanespeed_verdict.py 978c699e (all unchanged).
- Dry checks lanespeed_c56brief_check.py 9d181b65 and lanespeed_c56brief_odd_check.py 2434e6dc (unchanged).
  Workflow lanespeed-c56brief-q8-odd.yml.

## Procedure
One job. Blocks A1 B1 A2 B2. Each block starts a fresh llama-server with identical flags in both arms:
`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub 512`. One unscored warm-up, then the 15 odd-index cases in suite
order, once each, streamed, `temperature 0, seed 42, max_tokens 120, enable_thinking false, cache_prompt false`,
scored by `slm_bench.score`.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (n = 15 cases per block).
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Quality gate: usable count of each B block >= the lower of the two A blocks (same 15 odd cases).
- Descriptive: TTFT p50, prompt_ms p50, prompt_n, paired per-case difference, text equality, load time, which cases are
  unusable per block; side by side with C56-brief-odd (Q4_K_M, same cases) and C56-brief-q8 (Q8_0, even cases), never
  pooled.

## Decision rule (lanespeed_verdict.py, unchanged)
- NULL if a block is missing, incomplete or has a request error. One infra fix + one rerun allowed, then NULL.
- REVERT if the quality gate fails.
- KEEP if p50(A) - p50(B) > A/A spread AND both B block p50s are below both A block p50s AND the gate passes.
- NO_GAIN otherwise.

## Stop rule
One scored run. If it fails for an infrastructure reason (runner, download, deadline), the verdict is NULL with that
reason; one rerun after a documented infra fix is allowed, then NULL. No other reruns, whatever the outcome.

## What a KEEP / REVERT means
KEEP: together with C56-brief-q8, the e5 gate and the latency halving hold on all 30 slm-v2 cases at Q8_0 on this CPU.
REVERT: e5 loses a usable case on the odd half at Q8_0; Q8 lanes keep the full brief. Nothing here speaks for phones,
Metal or GPU lanes.
