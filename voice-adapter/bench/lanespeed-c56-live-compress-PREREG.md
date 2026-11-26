# C56-live-compress prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU, Neoverse-N2 in every earlier C56 run). A CPU result here says nothing about phones, Metal or GPUs.

## Why this run
C56-brief / -odd / -q8 / -q8-odd (#148-#151) measured that the e5 brief (2810 chars) about halves cold first-answer
latency against the full slm-v2 brief (5540 chars) with the usable gate unchanged. e5 is built by `lane_brief_e5.py`
from hand-listed fact markers ("dentist", "Maya's flight", ...) and a fixed filler-pair index list, so it only works on
this one fixture and has no deploy target. This run tests a deterministic compressor that needs no fixture knowledge.

## The compressor (frozen: voice-adapter/brief_compress.py)
`compress_brief(text, budget_chars)`: no model call, no markers. It keeps a verbatim, in-order subset of lines:
header lines, trailer lines (e.g. `Status:`), then whole exchanges greedily if they fit, in priority order: newest
exchange; "fact" exchanges (reply <= 200 chars) newest first; other exchanges newest first. No speaker structure, or a
header+trailer over budget: newest `budget_chars` characters. A brief already within budget is returned unchanged.
Unit tests (voice-adapter/test_brief_compress.py, written first, red before the module existed): same bytes on repeat,
<= budget at 7 budgets on 2 briefs, header/Status/newest exchange kept, all 6 planted context facts + the adb standing
approval kept, whole exchanges only, all 30 slm-v2 lane requests shorter by > 2000 chars with case turns intact, and a
second unrelated synthetic chat (other speaker names, other facts) keeps its 4 planted facts.

## Offline dev check (no model; `lane_brief_live.py --dev`)
Budget = 2810 (= e5 length). Output 2777 chars (-33 vs e5 on every one of the 30 lane system prompts; full 6273 vs
live 3510 for no-turn cases). Same 6 fact pairs, header and Status as e5; filler kept = the 5 newest filler pairs
(rust, stretch, CRDTs, sourdough, Japanese = filler 7-11) instead of e5's filler 2,4,6,8,10 (shared: stretch,
sourdough; e5-only: heat pump, sci-fi, git; live-only: rust, CRDTs, Japanese). No tuning was needed, so no case (even or odd)
was used to choose parameters; FACT_REPLY_CHARS = 200 was set from the rule's intent before the dev table. Frozen.

## Hypothesis
H_C56-live: on the 15 odd-index slm-v2 cases at gemma-4-E2B-it Q4_K_M, B (live-compressed brief) has a pooled
first-answer p50 lower than A (full brief) by more than the A/A spread, both B block p50s are below both A block p50s,
and usable(B_i) >= usable(A_i) for i = 1, 2 (paired, per block) and >= min(A1, A2) (frozen script).
Prior (I, not a decision input): about -48 % as e5 on the same cases (27.82 -> 14.24 s in C56-brief-odd), since the
prompt is ~1 % shorter than e5; the gate risk is the different filler choice (s-mom, f-everest were unusable in both
arms in C56-brief-odd).

## The one change vs C56-brief-odd (run 45773497094)
Arm B's suite: `slm-v2-live.json` (built by `lane_brief_live.py`, checked in CI with `--check`) instead of
`slm-v2-e5.json`. Model (Q4_K_M 740185b2), build b12527, server flags, sampling, cases (`--every 2 --offset 1`),
scorer, lanespeed.py and lanespeed_verdict.py are byte-identical. Workflow lanespeed-c56-live-compress.yml is a copy of
lanespeed-c56brief-odd.yml with only names, arm-B suite/sha, two added no-model checks and the trigger changed.
Trigger: the push that adds the workflow (dispatch by file name 404s off the default branch); workflow_dispatch is
also declared.

## Lessons carried from #148-#151
1. 45 min job cap (C56 AMEND-1): bench step 36 min, in-script deadline 1980 s; a block that hits the deadline makes
   the run incomplete -> NULL, never a partial verdict.
2. Cold prefill costs ~27-28 s per case for the full brief on this CPU (Q4_K_M: A p50 27.65 / 27.82 s), ~14 s for a
   2.8 k brief; 4 blocks x 15 cases measured 1352-1354 s wall. 30 cases per block (~2700 s) would not fit, so this
   run scores the 15 odd-index cases only (the half where s-mom, the GH9 risk case, lives).
3. Block sizing: ABAB, one fresh llama-server per block, one unscored warm-up (case 0 with the arm's brief), then the
   cases once each in suite order.
4. Usable gate = slm_bench.score: correct (answer regex, or SKIP for must-skip cases) AND spoken (no markdown,
   <= 45 words, <= 3 sentences). Count per block of 15.
5. A/A rule: noise band = |p50(A1) - p50(A2)|; prior A/A spreads 0.019-0.038 s, B/B 0.034-0.101 s.
6. One change per run; text equality A1-A2 / B1-B2 is greedy-deterministic (15/15 before) and is a wiring check.
7. Dispatch by file name fails off the default branch; the scored run starts from the push that adds the workflow.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks.
- Noise band: A/A spread. B/B spread reported.
- Gate: usable(B1) >= usable(A1) AND usable(B2) >= usable(A2) (paired), plus the frozen script's
  min(B) >= min(A).
- Descriptive: TTFT, prompt_ms, prompt_n, paired per-case diff, text equality, per-case unusable list; side by side
  with C56-brief-odd (e5, same cases and model), never pooled.

## Decision rule
- NULL: a block missing/incomplete/has a request error, or the run does not finish. One infra fix + one rerun, then NULL.
- REVERT: either gate fails.
- KEEP: p50(A) - p50(B) > A/A spread AND both B block p50s below both A block p50s AND both gates pass.
- NO_GAIN otherwise.

## Stop rule
One scored run. No reruns for outcome reasons.

## What a KEEP means
A deterministic, fixture-free compressor gives the e5-class cold latency cut at equal usable on these synthetic cases,
so production lanes have a deploy target (wiring it into tiers.snapshot is a separate change; defaults unchanged here).
