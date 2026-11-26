# C56-brief-q8 prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU; Neoverse-N2 in every earlier arm run, including C56, C56-brief and C56-brief-odd). A CPU result here verdicts
nothing about phones, Metal or GPUs.

## Why this run
C56-brief (run 45767553751, KEEP) and C56-brief-odd (run 45773497094, KEEP) showed, at Q4_K_M, that the e5 brief
roughly halves cold first-answer latency (p50 27.65 -> 13.94 s even half, 27.82 -> 14.25 s odd half) with an unchanged
usable count on all 30 slm-v2 cases. Q8_0 was never measured in this harness. In the GH11 text climb, e5 failed the
Q8_0 lane bar by one item (r-apples, an even-index case: full brief usable, e5 not). This run asks whether e5 keeps
the usable gate and the latency halving when the model file is Q8_0.

## Hypothesis
H_C56b-q8: on the 15 even-index slm-v2 cases, with gemma-4-E2B-it Q8_0 in both arms and everything else as in
C56-brief, B (e5 brief) has a first-answer sentence p50 lower than A (full brief) by more than the A/A spread, both B
block p50s are below both A block p50s, and usable(each B block) >= the lower usable of the two A blocks.
Prior (I, not a decision input): latency should halve as at Q4 (prefill-bound, prompt_n -41 %); the gate is at risk
because GH11 saw e5 lose r-apples at Q8_0 (one greedy sample per cell, different harness: warm prompt cache).

## The one change vs C56-brief
The GGUF file, in BOTH arms: gemma-4-E2B-it-Q8_0.gguf instead of gemma-4-E2B-it-Q4_K_M.gguf, same repo
(unsloth/gemma-4-E2B-it-GGUF) and same revision 0314792d. sha256 605d3c26 (5048352864 bytes) = the HF LFS oid at that
revision (checked against the HF paths-info API) and the file used as gemma4-e2b Q8_0 in GH2-GH11; checked again on the
runner before the bench. Case selection is the C56-brief one (`--every 2`, default offset 0: f-capital, f-light,
f-austen, r-apples, r-order, r-percent, c-dentist, c-budget, c-grok, u-fahrenheit, u-wait, s-screen, s-spotify,
s-build, s-email). The unscored warm-up stays case 0 (f-capital) with the arm's brief. Arms, build, server flags,
sampling, scorer and verdict script are unchanged (lanespeed_verdict.py byte-identical to C56, C56-brief and
C56-brief-odd). Workflow-only infra differences: dispatch-only trigger (no push trigger) and a 15 min download step
timeout for the 5.05 GB file (the text-climb value for the same file); neither touches the measurement.

## Time budget (lesson from C56 AMEND-1: the 45 min job cap)
Estimated from the GH11 runs 45061298454 / 45061314078 (same runner family, same llama.cpp build), cold first-answer
time of the first slm-v2 case, Q8_0 vs Q4_K_M (M): full brief 24.25 / 24.28 s vs 26.22 / 26.10 s; e5 brief 12.33 /
12.29 s vs 13.28 / 13.31 s. Q8_0 prefill on this CPU is about 0.93x the Q4_K_M time (I). Predicted bench wall:
0.93 x 1353.9 s (C56-brief measured) + about 20 s sha256 of the 5 GB file + a few s extra load per block = about
1300 s (I). This is below the 1900 s threshold, so the design keeps all 15 even-index cases in 4 blocks (no case
reduction). In-script deadline 1980 s (about 680 s margin), bench step 36 min, job cap 45 min, download step 15 min.

## Arms and frozen inputs (sha256 in lanespeed-c56brief-q8-FREEZE.sha256)
- A: voice-adapter/bench/slm-v2.json 7fa9f7c6 (full brief, 5540 chars). B: voice-adapter/bench/slm-v2-e5.json
  add17576 (e5 brief, 2810 chars, GH11 rule; cases byte-identical to A).
- Model: unsloth/gemma-4-E2B-it-GGUF rev 0314792d, gemma-4-E2B-it-Q8_0.gguf sha256 605d3c26 (checked on the runner).
- llama.cpp b12527 ubuntu-arm64 prebuilt, tarball sha256 c2758c87.
- slm_bench.py 805a2518, tiers.py 8496ab2c, lanespeed.py 02bd44d6, lanespeed_verdict.py 978c699e (all unchanged).
- Dry checks lanespeed_c56brief_check.py 9d181b65 and lanespeed_c56brief_odd_check.py 2434e6dc (unchanged; the latter
  also proves the default selection equals the C56-brief even case_ids). Workflow lanespeed-c56brief-q8.yml.

## Procedure
One job. Blocks A1 B1 A2 B2. Each block starts a fresh llama-server with identical flags in both arms:
`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub 512`. One unscored warm-up, then the 15 even-index cases in suite
order, once each, streamed, `temperature 0, seed 42, max_tokens 120, enable_thinking false, cache_prompt false`,
scored by `slm_bench.score`.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (n = 15 cases per block).
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Quality gate: usable count of each B block >= the lower of the two A blocks (same 15 even cases).
- Descriptive: TTFT p50, prompt_ms p50, prompt_n, paired per-case difference, faster count, text equality, load time,
  which cases are unusable per block; side by side with C56-brief (Q4_K_M, same cases), never pooled.

## Decision rule (lanespeed_verdict.py, unchanged)
- NULL if a block is missing, incomplete or has a request error. One infra fix + one rerun allowed, then NULL.
- REVERT if the quality gate fails.
- KEEP if p50(A) - p50(B) > A/A spread AND both B block p50s are below both A block p50s AND the gate passes.
- NO_GAIN otherwise.

## Stop rule
One scored run. If it fails for an infrastructure reason (runner, download, deadline), the verdict is NULL with that
reason; one rerun after a documented infra fix is allowed, then NULL. No other reruns, whatever the outcome.

## What a KEEP / REVERT means
KEEP: the e5 gate and the latency gain hold at Q8_0 on the even half on this CPU (the odd half at Q8_0 stays
unmeasured). REVERT: e5 is a Q4_K_M-only CPU lane brief candidate; Q8 lanes keep the full brief. Nothing here speaks
for phones, Metal or GPU lanes.
