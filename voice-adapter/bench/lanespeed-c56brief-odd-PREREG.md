# C56-brief-odd prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU; Neoverse-N2 in every earlier arm run, including C56 and C56-brief). A CPU result here verdicts nothing about
phones, Metal or GPUs.

## Why this run
C56-brief (run 45767553751, KEEP) measured the e5 brief on the 15 even-index slm-v2 cases only: first-answer p50
27.653 -> 13.944 s, usable 14/14/14/14. The usable gate has never been measured on the other 15 cases. Before e5 can
be a lane brief candidate, the gate must also hold on the odd-index cases. This run is that out-of-sample check.

## Hypothesis
H_C56b-odd: on the 15 odd-index slm-v2 cases, with everything else as in C56-brief, B (e5 brief) has a first-answer
sentence p50 lower than A (full brief) by more than the A/A spread, both B block p50s are below both A block p50s, and
usable(each B block) >= the lower usable of the two A blocks.

## The one change vs C56-brief
Case selection only: `lanespeed.py --every 2 --offset 1` scores suite indices 1, 3, ..., 29 (f-everest, f-planet,
f-gold, r-mult, r-train, r-days, c-maya, c-engine, c-ram, u-australia, u-correct, s-text, s-files, s-mom, s-weather;
3 fact, 3 reasoning, 3 context, 2 followup, 4 must-SKIP, the same kind mix as the even half). `--offset` is new; its
default 0 gives the same selection and the same receipt keys as before (checked by lanespeed_c56brief_odd_check.py
against the C56-brief case_ids). The unscored warm-up stays case 0 (f-capital) with the arm's brief, as before.
Arms, model, build, server flags, sampling, scorer and verdict script are unchanged (lanespeed_verdict.py is
byte-identical to C56 and C56-brief).

## Time budget (lesson from C56 AMEND-1: the 45 min job cap)
Cold prefill is about 28 s per case for A and about 14 s for B (C56-brief, measured). The odd cases have the same mean
prompt length as the even cases (6322 vs 6324 chars for A, 3592 vs 3594 for B, measured offline with
slm_bench.messages), so the predicted wall time is the C56-brief wall time: 2 x 16 x 28 + 2 x 16 x 14 + loads, about
1350 s (C56-brief measured 1353.9 s). In-script deadline 1980 s (about 630 s of margin), bench step 36 min, job cap
45 min. Unchanged from C56-brief.

## Arms and frozen inputs (sha256 in lanespeed-c56brief-odd-FREEZE.sha256)
- A: voice-adapter/bench/slm-v2.json 7fa9f7c6 (full brief, 5540 chars). B: voice-adapter/bench/slm-v2-e5.json
  add17576 (e5 brief, 2810 chars, GH11 rule; cases byte-identical to A).
- Model: unsloth/gemma-4-E2B-it-GGUF rev 0314792d, gemma-4-E2B-it-Q4_K_M.gguf sha256 740185b2 (checked on the runner).
- llama.cpp b12527 ubuntu-arm64 prebuilt, tarball sha256 c2758c87.
- slm_bench.py 805a2518, tiers.py 8496ab2c, lanespeed_verdict.py 978c699e (all unchanged).
- lanespeed.py (C56-brief + `--offset` only), workflow lanespeed-c56brief-odd.yml, dry checks
  lanespeed_c56brief_check.py (unchanged) and lanespeed_c56brief_odd_check.py.

## Procedure
One job. Blocks A1 B1 A2 B2. Each block starts a fresh llama-server with identical flags in both arms:
`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub 512`. One unscored warm-up, then the 15 odd-index cases in suite
order, once each, streamed, `temperature 0, seed 42, max_tokens 120, enable_thinking false, cache_prompt false`,
scored by `slm_bench.score`.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (n = 15 cases per block).
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Quality gate: usable count of each B block >= the lower of the two A blocks (same 15 odd cases).
- Descriptive: TTFT p50, prompt_ms p50, prompt_n (A from verdict, B from block summaries), paired per-case difference,
  text equality, load time, which cases are unusable per block.

## Decision rule (lanespeed_verdict.py, unchanged)
- NULL if a block is missing, incomplete or has a request error. One infra fix + one rerun allowed, then NULL.
- REVERT if the quality gate fails.
- KEEP if p50(A) - p50(B) > A/A spread AND both B block p50s are below both A block p50s AND the gate passes.
- NO_GAIN otherwise.

## Stop rule
One scored run. No pooling with C56-brief (the two halves are reported side by side, never merged into a new verdict).
If the run fails for an infrastructure reason (runner, download, deadline), the verdict is NULL with that reason; one
rerun after a documented infra fix is allowed, then NULL. No other reruns, whatever the outcome.

## What a KEEP means
Together with C56-brief, the e5 gate then holds on all 30 slm-v2 cases at Q4_K_M on this CPU. It stays a candidate
lane brief for CPU Q4 only: Q8 is not covered (e5 failed the GH11 Q8 bar by one item), and nothing here speaks for
phones, Metal or GPU lanes.
