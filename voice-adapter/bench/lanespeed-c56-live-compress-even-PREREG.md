# C56-live-compress-even prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU, Neoverse-N2 in every earlier C56 run). A CPU result here says nothing about phones, Metal or GPUs.

## Why this run
C56-live-compress (run 45850240823, PR #154) scored the 15 odd-index slm-v2 cases: full brief p50 27.806 s vs
compress_brief brief 14.097 s, 15/15 cases faster, usable 13 in every block -> KEEP. Those are half the suite. This run
scores the other half (the 15 even-index cases, the same half C56-brief measured with the hand-built e5 brief in run
45767553751), so the compressor is measured on all 30 cases with no case used twice.

## The one change vs C56-live-compress (run 45850240823)
Case selection `--every 2 --offset 0` (15 even-index cases) instead of `--every 2 --offset 1`. Nothing else.
Compressor (brief_compress.py 485d927b), arm-B suite slm-v2-live.json 69662e3f, arm-A suite slm-v2.json 7fa9f7c6,
model gemma-4-E2B-it Q4_K_M 740185b2, llama.cpp b12527, server flags, sampling, scorer (slm_bench.py), lanespeed.py,
lanespeed_verdict.py and both dry checks are byte-identical (lanespeed-c56-live-compress-even-FREEZE.sha256 checks them
against the odd run's freeze). Workflow lanespeed-c56-live-compress-even.yml is a copy of lanespeed-c56-live-compress.yml
with only names, branch/path trigger, receipt/artifact names and the offset changed; timeouts unchanged (job 45 min,
bench step 36 min, in-script deadline 1980 s). Trigger: the push that adds the workflow; workflow_dispatch also declared.
Note: case 0 is both the unscored warm-up and the first scored case (as in C56-brief); the warm-up stays unscored.

## Hypothesis
H_C56-live-even: on the 15 even-index slm-v2 cases, B (compressed brief) has a pooled first-answer p50 lower than A
(full brief) by more than the A/A spread, both B block p50s are below both A block p50s, and usable(B_i) >= usable(A_i)
for i = 1, 2 (paired, per block) and min(B) >= min(A) (frozen script).
Prior (I, not a decision input): about -49 % as in the odd run and in C56-brief (even, e5: 27.653 -> 13.944 s, usable
14/14); gate risk is the filler choice differing from e5 (e5 kept filler 2,4,6,8,10; compress keeps the 5 newest).

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks. n = 15 cases per block.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Gate: usable(B1) >= usable(A1) AND usable(B2) >= usable(A2) (paired), plus the frozen script's min(B) >= min(A).
  Usable = slm_bench.score: correct AND spoken (no markdown, <= 45 words, <= 3 sentences).
- Descriptive: TTFT, prompt_n, paired per-case diff, cases faster, text equality, per-case unusable list; side by side
  with C56-brief (e5, same even cases) never pooled; pooled odd+even 30-case view with C56-live-compress (descriptive
  only, not a decision input).

## Decision rule (identical to C56-live-compress)
- NULL: a block missing/incomplete/has a request error, or the run does not finish. One infra fix + one rerun, then NULL.
- REVERT: either gate fails.
- KEEP: p50(A) - p50(B) > A/A spread AND both B block p50s below both A block p50s AND both gates pass.
- NO_GAIN otherwise.

## Stop rule
One scored run. No reruns for outcome reasons. A failing push-triggered "z0live checks" run does not affect the verdict.

## What a KEEP means
The odd-half KEEP of the deterministic compressor holds on the other half of the synthetic suite, so the cold latency
cut at equal usable covers all 30 slm-v2 cases. Production defaults stay unchanged (tiers.snapshot not wired here).
