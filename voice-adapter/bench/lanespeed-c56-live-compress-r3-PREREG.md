# C56-live-compress-r3 prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU, Neoverse-N2 in every earlier C56 run). A CPU result here says nothing about phones, Metal or GPUs.

## Why this run
C56-live-compress-even (run 45858171167, PR #156): p50 27.592 -> 13.928 s, but usable A 14/14 vs B 13/13 -> REVERT.
The must-skip case s-build ("Did my last build pass?") flips: full brief answers SKIP, compressed brief answers a spoken
non-answer. No brief holds a SKIP instruction (it is in tiers.SYSTEM, identical in both arms). The one difference vs the
hand-built e5 brief (which passed all even cases in C56-brief, run 45767553751) is filler choice: e5 keeps filler
exchanges heat pump, sci-fi books, git passphrase; compress_brief kept rust CLI, CRDTs (newest filler first).

## The one change vs C56-live-compress-even (run 45858171167)
brief_compress.compress_brief fills the leftover budget with filler spread evenly over the chat: every other filler
exchange counted back from the newest filler (newest first), then the skipped fillers only if budget is left. Was: all
filler newest first. Budget 2810 unchanged. Header, Status line, newest exchange and all 6 fact exchanges still kept.
New arm-B brief: 2672 chars (was 2777). Kept filler (0-based exchange index): 6 sci-fi books, 9 git passphrase,
12 stretch, 15 sourdough; plus the newest exchange 17 Japanese (always kept). e5 filler: 3, 6, 9, 12, 15 -> overlap 4/5
(missing 3 heat pump; live adds 17 Japanese, which the rule always keeps). Was: 10, 12, 13, 15 (+17).
Everything else byte-identical: arm-A suite slm-v2.json, cases (--every 2 --offset 0, 15 even-index cases), model
gemma-4-E2B-it Q4_K_M 740185b2, llama.cpp b12527, server flags, sampling, scorer (slm_bench.py, tiers.py), lanespeed.py,
lanespeed_verdict.py (978c699e), both dry checks, timeouts. Workflow lanespeed-c56-live-compress-r3.yml is a copy of the
even workflow with only names, trigger branch/path, artifact names and SUITE_B_SHA changed. Trigger: the push that adds it.

## Hypothesis
H_C56-live-r3: on the 15 even-index slm-v2 cases, B (spread-filler compressed brief) has a pooled first-answer p50 lower
than A (full brief) by more than the A/A spread, both B block p50s below both A block p50s, usable(B_i) >= usable(A_i)
for i = 1, 2 (paired, per block) and min(B) >= min(A) (frozen script). Secondary (descriptive): s-build answered SKIP in B.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (ABAB A1 B1 A2 B2, fresh
  llama-server per block). n = 15 cases per block.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Gate: usable(B1) >= usable(A1) AND usable(B2) >= usable(A2) (paired), plus the frozen script's min(B) >= min(A).
  Usable = slm_bench.score: correct AND spoken.

## Decision rule (identical to C56-live-compress / -even)
- NULL: a block missing/incomplete/has a request error, or the run does not finish. One infra fix + one rerun, then NULL.
- REVERT: either gate fails.
- KEEP: p50(A) - p50(B) > A/A spread AND both B block p50s below both A block p50s AND both gates pass.
- NO_GAIN otherwise.

## Stop rule
One scored run. No reruns for outcome reasons.
